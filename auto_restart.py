#!/usr/bin/env python3
"""Safely restart the Mac after extended uptime and prolonged sleep/inactivity.

Evaluates:
  1. System uptime >= 14 days (1,209,600s).
  2. Sleep / idle duration >= 18 hours (64,800s).
  3. AC power connected.
  4. No blocking background operations (e.g. active Time Machine backups).

When eligible:
  Prefers `fdesetup authrestart` (if FileVault is active) so the Mac reboots
  directly back into macOS without stalling at the pre-boot FileVault prompt,
  falling back to `shutdown -r now` or AppleScript restart.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from idle_config import APP_SUPPORT_DIR, load_config
from idle_gate import read_idle_seconds

STATE_DIR = Path(APP_SUPPORT_DIR)
STATE_FILE = STATE_DIR / "auto-restart-state.json"
LOCK_FILE = STATE_DIR / "auto-restart.lock"

DEFAULT_POLICY = {
    "auto_restart_enabled": True,
    "auto_restart_uptime_days": 14,
    "auto_restart_sleep_hours": 18,
    "auto_restart_idle_hours": 18,
    "auto_restart_require_ac": True,
    "auto_restart_use_authrestart": True,
}

PMSET = "/usr/bin/pmset"
SYSCTL = "/usr/sbin/sysctl"
SHUTDOWN = "/sbin/shutdown"
FDESETUP = "/usr/bin/fdesetup"
TMUTIL = "/usr/bin/tmutil"


def get_uptime_seconds(
    runner: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], float] = time.time,
) -> float:
    """Read system uptime in seconds using kern.boottime."""
    try:
        res = runner([SYSCTL, "-n", "kern.boottime"], capture_output=True, text=True, check=False, timeout=5)
        if res.returncode == 0 and res.stdout:
            match = re.search(r"sec\s*=\s*(\d+)", res.stdout)
            if match:
                boot_sec = int(match.group(1))
                return max(0.0, now_fn() - boot_sec)
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except OSError:
        pass

    return 0.0


def is_ac_power(runner: Callable[..., Any] = subprocess.run) -> bool:
    """Check if Mac is connected to AC power."""
    try:
        res = runner([PMSET, "-g", "batt"], capture_output=True, text=True, check=False, timeout=5)
        if res.returncode == 0:
            return "AC Power" in (res.stdout or "")
    except (OSError, subprocess.SubprocessError):
        pass
    return True


def is_backup_active(runner: Callable[..., Any] = subprocess.run) -> bool:
    """Check if Time Machine is currently running."""
    try:
        res = runner([TMUTIL, "status"], capture_output=True, text=True, check=False, timeout=5)
        if res.returncode == 0 and res.stdout:
            running_match = re.search(r"Running\s*=\s*1", res.stdout)
            return bool(running_match)
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def is_filevault_active(runner: Callable[..., Any] = subprocess.run) -> bool:
    """Check if FileVault disk encryption is enabled."""
    try:
        res = runner([FDESETUP, "isactive"], capture_output=True, text=True, check=False, timeout=5)
        return res.returncode == 0 and "true" in (res.stdout or "").lower()
    except (OSError, subprocess.SubprocessError):
        return False


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def evaluate_restart_eligibility(
    config: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    now: float | None = None,
    runner: Callable[..., Any] = subprocess.run,
    idle_reader: Callable[[], float | None] = read_idle_seconds,
) -> dict[str, Any]:
    """Evaluate whether the Mac satisfies all auto-restart criteria."""
    now_time = time.time() if now is None else now
    cfg = dict(DEFAULT_POLICY)
    if config:
        sub_cfg = config.get("auto_restart", {})
        if isinstance(sub_cfg, dict):
            if "enabled" in sub_cfg:
                cfg["auto_restart_enabled"] = sub_cfg["enabled"]
            if "uptime_days" in sub_cfg:
                cfg["auto_restart_uptime_days"] = sub_cfg["uptime_days"]
            if "sleep_hours" in sub_cfg:
                cfg["auto_restart_sleep_hours"] = sub_cfg["sleep_hours"]
            if "idle_hours" in sub_cfg:
                cfg["auto_restart_idle_hours"] = sub_cfg["idle_hours"]
            if "require_ac" in sub_cfg:
                cfg["auto_restart_require_ac"] = sub_cfg["require_ac"]
            if "use_authrestart" in sub_cfg:
                cfg["auto_restart_use_authrestart"] = sub_cfg["use_authrestart"]
        cfg.update({k: v for k, v in config.items() if k.startswith("auto_restart_")})

    uptime_threshold = float(cfg.get("auto_restart_uptime_days", 14)) * 86400
    sleep_threshold = float(cfg.get("auto_restart_sleep_hours", 18)) * 3600
    idle_threshold = float(cfg.get("auto_restart_idle_hours", 18)) * 3600
    require_ac = bool(cfg.get("auto_restart_require_ac", True))

    uptime_sec = get_uptime_seconds(runner=runner, now_fn=lambda: now_time)
    on_ac = is_ac_power(runner=runner)
    backup_active = is_backup_active(runner=runner)

    # User HID inactivity
    try:
        raw_idle = idle_reader()
        hid_idle_sec = float(raw_idle) if raw_idle is not None else 0.0
    except Exception:
        hid_idle_sec = 0.0

    # Sleep gap duration via recorded heartbeat
    current_state = load_state() if state is None else state
    last_heartbeat = float(current_state.get("last_heartbeat", 0.0))
    if last_heartbeat > 0.0:
        heartbeat_gap_sec = max(0.0, now_time - last_heartbeat)
    else:
        heartbeat_gap_sec = 0.0

    # A system is considered asleep/unattended if either:
    # 1. The heartbeat gap shows the system was suspended/sleeping for >= sleep_threshold.
    # 2. Or the HID idle time confirms no human keyboard/mouse interaction for >= idle_threshold.
    sleep_or_idle_satisfied = (
        (sleep_threshold > 0 and heartbeat_gap_sec >= sleep_threshold) or
        (idle_threshold > 0 and hid_idle_sec >= idle_threshold)
    )

    reasons: list[str] = []
    if not cfg.get("auto_restart_enabled", True):
        reasons.append("auto-restart is disabled in configuration")
    if uptime_sec < uptime_threshold:
        days_current = uptime_sec / 86400
        days_target = uptime_threshold / 86400
        reasons.append(f"uptime {days_current:.1f}d < required {days_target:.1f}d")
    if not sleep_or_idle_satisfied:
        hours_gap = heartbeat_gap_sec / 3600
        hours_idle = hid_idle_sec / 3600
        reasons.append(f"sleep gap ({hours_gap:.1f}h) and idle time ({hours_idle:.1f}h) are both < threshold ({sleep_threshold / 3600:.1f}h)")
    if require_ac and not on_ac:
        reasons.append("system is on battery power")
    if backup_active:
        reasons.append("Time Machine backup is actively running")

    eligible = len(reasons) == 0

    return {
        "eligible": eligible,
        "reasons": reasons,
        "uptime_seconds": uptime_sec,
        "uptime_days": round(uptime_sec / 86400, 2),
        "heartbeat_gap_seconds": heartbeat_gap_sec,
        "heartbeat_gap_hours": round(heartbeat_gap_sec / 3600, 2),
        "hid_idle_seconds": hid_idle_sec,
        "hid_idle_hours": round(hid_idle_sec / 3600, 2),
        "on_ac": on_ac,
        "backup_active": backup_active,
        "timestamp": now_time,
    }


def execute_restart(
    dry_run: bool = False,
    prefer_authrestart: bool = True,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[bool, str]:
    """Execute system restart safely."""
    if dry_run:
        return True, "dry-run: restart command was skipped"

    # 1. If FileVault is active and authrestart preferred, try fdesetup authrestart
    if prefer_authrestart and is_filevault_active(runner=runner):
        try:
            res = runner([FDESETUP, "authrestart"], capture_output=True, text=True, check=False, timeout=10)
            if res.returncode == 0:
                return True, "initiated via fdesetup authrestart"
        except (OSError, subprocess.SubprocessError):
            pass

    # 2. Try shutdown -r now (works when running as root LaunchDaemon)
    try:
        res = runner([SHUTDOWN, "-r", "now"], capture_output=True, text=True, check=False, timeout=10)
        if res.returncode == 0:
            return True, "initiated via shutdown -r now"
    except (OSError, subprocess.SubprocessError):
        pass

    # 3. Fallback to AppleScript graceful restart
    try:
        script = 'tell application "System Events" to restart'
        res = runner(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False, timeout=10)
        if res.returncode == 0:
            return True, "initiated via AppleScript restart"
    except (OSError, subprocess.SubprocessError):
        pass

    return False, "failed to trigger restart (insufficient privileges or execution blocked)"


def run_cycle(
    dry_run: bool = False,
    force: bool = False,
    config: dict[str, Any] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Run an evaluation cycle, update heartbeat state, and restart if eligible."""
    now = time.time()
    state = load_state()

    eval_result = evaluate_restart_eligibility(config=config, state=state, now=now, runner=runner)

    # Always update heartbeat
    state["last_heartbeat"] = now
    state["last_check"] = now
    state["last_check_status"] = eval_result
    save_state(state)

    should_restart = force or eval_result["eligible"]
    if not should_restart:
        return 1

    prefer_auth = config.get("auto_restart_use_authrestart", True) if config else True
    success, msg = execute_restart(dry_run=dry_run, prefer_authrestart=prefer_auth, runner=runner)

    state["last_restart_attempt"] = now
    state["last_restart_result"] = {"success": success, "message": msg, "dry_run": dry_run}
    save_state(state)

    return 0 if success else 2


def generate_launchd_plist(script_path: str | None = None) -> str:
    path = script_path or os.path.abspath(__file__)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.john.idle-maintenance.auto-restart</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{path}</string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>WakeToRun</key>
    <true/>
    <key>ProcessType</key>
    <string>Maintenance</string>
    <key>StandardOutPath</key>
    <string>/Users/john/Library/Logs/idle-maintenance/auto-restart.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/john/Library/Logs/idle-maintenance/auto-restart.err.log</string>
</dict>
</plist>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate and trigger automatic maintenance reboot.")
    parser.add_argument("--status", action="store_true", help="Print current status and criteria evaluation.")
    parser.add_argument("--check", action="store_true", help="Check eligibility and exit (0=eligible, 1=not).")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate criteria and simulate restart.")
    parser.add_argument("--force", action="store_true", help="Bypass criteria and initiate restart immediately.")
    parser.add_argument("--print-plist", action="store_true", help="Print LaunchDaemon plist definition.")

    args = parser.parse_args(argv)

    if args.print_plist:
        sys.stdout.write(generate_launchd_plist())
        return 0

    base_dir = os.path.dirname(os.path.abspath(__file__))
    config = load_config(base_dir)

    if args.status:
        eval_res = evaluate_restart_eligibility(config=config)
        print("Idle Maintenance Auto-Restart Status:")
        print(f"  Eligible:          {eval_res['eligible']}")
        print(f"  Uptime:            {eval_res['uptime_days']} days ({eval_res['uptime_seconds']:.0f}s)")
        print(f"  Sleep Gap:         {eval_res['heartbeat_gap_hours']} hours ({eval_res['heartbeat_gap_seconds']:.0f}s)")
        print(f"  HID Inactivity:    {eval_res['hid_idle_hours']} hours ({eval_res['hid_idle_seconds']:.0f}s)")
        print(f"  Power Source:      {'AC Power' if eval_res['on_ac'] else 'Battery'}")
        print(f"  Backup Running:    {eval_res['backup_active']}")
        if eval_res["reasons"]:
            print("  Ineligibility Reasons:")
            for r in eval_res["reasons"]:
                print(f"    - {r}")
        return 0

    if args.check:
        eval_res = evaluate_restart_eligibility(config=config)
        return 0 if eval_res["eligible"] else 1

    return run_cycle(dry_run=args.dry_run, force=args.force, config=config)


if __name__ == "__main__":
    sys.exit(main())
