#!/usr/bin/env python3
"""Safely start native Time Machine backups under the existing idle monitor.

This is a one-shot worker, never a resident watcher. It cannot disable Apple's
own Time Machine schedule. Enable it only after a verified dry deployment.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from idle_config import disk_busy_status, load_config
from idle_gate import read_idle_seconds

STATE_DIR = Path.home() / "Library/Application Support/idle-maintenance"
STATE_FILE = STATE_DIR / "time-machine-backup-state.json"
LOCK_FILE = STATE_DIR / "time-machine-backup.lock"
TMUTIL = "/usr/bin/tmutil"
PMSET = "/usr/bin/pmset"
BACKUP_TIMEOUT = 8 * 3600
BACKUP_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6})(?:\.backup)?")

DEFAULT_POLICY = {
    "enabled": False,
    "start_hour": 20,
    "end_hour": 7,
    "idle_minutes": 30,
    "min_interval_hours": 24,
    "failure_cooldown_hours": 3,
    "overdue_hours": 48,
    "alert_cooldown_hours": 24,
    "max_load_1min": 2.0,
}


def within_window(hour: int, start: int, end: int) -> bool:
    """A same start/end hour permits the full day; overnight ranges wrap."""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def backup_epoch(path: str | None, local_tz: Any) -> float | None:
    if not path:
        return None
    matches = BACKUP_DATE.findall(path)
    if not matches:
        return None
    try:
        return datetime.strptime(matches[-1], "%Y-%m-%d-%H%M%S").replace(
            tzinfo=local_tz
        ).timestamp()
    except ValueError:
        return None


def parse_running(output: str) -> bool | None:
    match = re.search(r"\bRunning\s*=\s*(true|false|1|0)\s*;", output, re.I)
    if not match:
        return None
    return match.group(1).lower() in {"true", "1"}


def _command(
    runner: Callable[..., Any], command: list[str], *, timeout: int = 12
) -> Any:
    return runner(command, capture_output=True, text=True, check=False, timeout=timeout)


def _latest_backup(runner: Callable[..., Any]) -> str | None:
    try:
        result = _command(runner, [TMUTIL, "latestbackup"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    paths = (result.stdout or "").strip().splitlines()
    latest = paths[-1].strip() if paths else ""
    return latest if latest.startswith("/") and BACKUP_DATE.search(latest) else None


def _destinations_configured(runner: Callable[..., Any]) -> bool:
    try:
        result = _command(runner, [TMUTIL, "destinationinfo"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(
        re.search(r"(?m)^\s*(?:Name|ID)\s*:", result.stdout or "")
    )


def _on_ac(runner: Callable[..., Any]) -> bool:
    try:
        result = _command(runner, [PMSET, "-g", "batt"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "AC Power" in (result.stdout or "")


def _backup_running(runner: Callable[..., Any]) -> bool | None:
    try:
        result = _command(runner, [TMUTIL, "status"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_running(result.stdout or "") if result.returncode == 0 else None


def read_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tm-backup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def notify_overdue(_title: str, message: str) -> None:
    """Best-effort local alert; no shell or permission elevation."""
    safe_message = message.replace('"', "'")
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{safe_message}" with title "Time Machine backup overdue"'],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def policy_from(config: dict[str, Any]) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    supplied = config.get("time_machine_backup", {})
    if isinstance(supplied, dict):
        policy.update(supplied)
    for key in ("start_hour", "end_hour"):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
            raise ValueError(f"time_machine_backup.{key} must be an hour from 0 to 23")
    for key in ("idle_minutes", "min_interval_hours", "failure_cooldown_hours",
                "overdue_hours", "alert_cooldown_hours", "max_load_1min"):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or value <= 0:
            raise ValueError(f"time_machine_backup.{key} must be positive")
    if not isinstance(policy["enabled"], bool):
        raise ValueError("time_machine_backup.enabled must be true or false")
    return policy


def run_once(
    *,
    config: dict[str, Any] | None = None,
    state_file: Path = STATE_FILE,
    lock_file: Path = LOCK_FILE,
    runner: Callable[..., Any] = subprocess.run,
    clock: Callable[[], datetime] | None = None,
    idle_reader: Callable[[], float | None] = read_idle_seconds,
    load_reader: Callable[[], tuple[float, float, float]] = os.getloadavg,
    disk_probe: Callable[..., dict[str, Any]] = disk_busy_status,
    notifier: Callable[[str, str], None] = notify_overdue,
) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    policy = policy_from(cfg)
    if not policy["enabled"]:
        return {"status": "disabled", "reason": "opt_in_required"}

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "deferred", "reason": "worker_already_running"}
        try:
            now = clock() if clock else datetime.now().astimezone()
            epoch = now.timestamp()
            state = read_state(state_file)
            state.setdefault("first_seen_at", epoch)
            previous = _latest_backup(runner)
            last_backup = backup_epoch(previous, now.tzinfo)
            if last_backup is None:
                last_backup = state.get("last_success_at")
            if not isinstance(last_backup, (int, float)):
                last_backup = None

            def finish(status: str, reason: str, **extra: Any) -> dict[str, Any]:
                state["last_decision"] = reason
                state["last_checked_at"] = epoch
                save_state(state_file, state)
                return {"status": status, "reason": reason, **extra}

            # Alert health independently of scheduling eligibility; rate-limit alerts.
            baseline = last_backup if last_backup is not None else state["first_seen_at"]
            if epoch - baseline >= policy["overdue_hours"] * 3600:
                if epoch - float(state.get("last_alert_at", 0)) >= policy["alert_cooldown_hours"] * 3600:
                    try:
                        notifier("Time Machine", f"No confirmed backup in the last {policy['overdue_hours']:g} hours. Check the backup disk.")
                    except Exception:
                        pass
                    state["last_alert_at"] = epoch

            if not within_window(now.hour, policy["start_hour"], policy["end_hour"]):
                return finish("deferred", "outside_window")
            if last_backup is not None and epoch - last_backup < policy["min_interval_hours"] * 3600:
                return finish("deferred", "backup_recent")
            if epoch - float(state.get("last_attempt_at", 0)) < policy["failure_cooldown_hours"] * 3600:
                return finish("deferred", "retry_cooldown")
            if not _on_ac(runner):
                return finish("deferred", "not_on_ac")
            idle = idle_reader()
            if idle is None or idle < policy["idle_minutes"] * 60:
                return finish("deferred", "not_idle")
            try:
                load = load_reader()[0]
            except (OSError, ValueError, IndexError, TypeError):
                return finish("deferred", "load_unavailable")
            if load > policy["max_load_1min"]:
                return finish("deferred", "cpu_busy")
            try:
                disk = disk_probe(cfg)
            except (OSError, ValueError, TypeError):
                disk = {}
            if not disk.get("available") or disk.get("busy"):
                return finish("deferred", "disk_busy_or_unknown")
            if not _destinations_configured(runner):
                return finish("deferred", "destination_unavailable")
            running = _backup_running(runner)
            if running is None or running:
                return finish("deferred", "backup_running_or_unknown")

            state["last_attempt_at"] = epoch
            state["last_result"] = "running"
            save_state(state_file, state)
            try:
                result = _command(
                    runner, [TMUTIL, "startbackup", "--auto", "--block"], timeout=BACKUP_TIMEOUT
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                state["last_result"] = "failed"
                state["last_error"] = f"Backup command failed or timed out: {type(exc).__name__}"
                return finish("error", "backup_command_failure")

            after = _latest_backup(runner)
            if result.returncode != 0 or not after or after == previous:
                state["last_result"] = "failed"
                state["last_error"] = (result.stderr or result.stdout or
                                       "No new verified backup")[:500]
                return finish("error", "backup_not_verified")

            state["last_result"] = "success"
            state["last_success_at"] = epoch
            state["last_backup_path"] = after
            state.pop("last_error", None)
            return finish("success", "verified_new_backup")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="Run once if all safety and schedule gates pass")
    parser.add_argument("--status", action="store_true",
                        help="Read saved status; never start a backup")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps({"policy": policy_from(load_config()),
                          "state": read_state()}, indent=2, sort_keys=True))
        return 0
    try:
        report = run_once()
    except (OSError, ValueError) as exc:
        report = {"status": "error", "reason": str(exc)}
    print(json.dumps(report, sort_keys=True))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
