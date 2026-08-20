#!/usr/bin/env python3
"""Report scheduled, resource-monitor, and interactive idle-maintenance status."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from idle_config import is_terminal_suggestion_time, keep_entry_is_active, load_config

LAUNCHD_LABEL = "com.john.idle-maintenance"
MONITOR_LABEL = "com.john.idle-maintenance-monitor"
MONITOR_STATE = "resource-monitor-state.json"
MONITOR_HISTORY = "resource-monitor-history.jsonl"
ATTRIBUTION_NOTE = (
    "I/O charged to the process during the sampled window; "
    "not definitive physical-disk attribution."
)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_recent_jsonl(path: Path, limit: int = 10) -> list[dict[str, Any]]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    values = []
    for line in lines[-max(1, limit):]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def expand_command(command: Any, home: Path) -> list[str]:
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list):
        return []
    expanded = []
    for part in command:
        value = os.path.expandvars(str(part))
        if value == "~" or value.startswith("~/"):
            value = str(home) + value[1:]
        expanded.append(value)
    return expanded


def resolve_runner_command(config: dict[str, Any], home: Path) -> list[str]:
    configured = expand_command(config.get("scheduled_runner_status_command", []), home)
    if configured:
        return configured
    wiki_root = os.environ.get("WIKI_PATH") or os.environ.get("WIKI_ROOT")
    root = Path(wiki_root).expanduser() if wiki_root else home / "wiki"
    runner = root / "99_meta/scripts/idle_maintenance_runner.sh"
    return [str(runner), "--status"] if runner.is_file() else []


def parse_launchctl(output: str, returncode: int) -> dict[str, Any]:
    state_match = re.search(r"^\s*state = (.+)$", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)$", output, re.MULTILINE)
    loaded = returncode == 0
    last_exit = int(exit_match.group(1)) if exit_match else None
    state = state_match.group(1).strip() if state_match else ("unloaded" if not loaded else "unknown")
    healthy = loaded and last_exit in {None, 0}
    return {
        "loaded": loaded,
        "state": state,
        "last_exit_code": last_exit,
        "healthy": healthy,
        "summary": (
            "Loaded and healthy (idle between scheduled runs)"
            if healthy and state == "not running"
            else "Loaded and healthy" if healthy else "Needs attention"
        ),
    }


def launchd_status(label: str, command_runner) -> dict[str, Any]:
    try:
        result = command_runner(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return parse_launchctl(result.stdout or result.stderr, result.returncode)
    except (OSError, subprocess.TimeoutExpired) as error:
        value = parse_launchctl(str(error), 1)
        value["error"] = str(error)
        return value


def latest_log_event(log_path: Path) -> str:
    try:
        lines = [line.strip() for line in log_path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "No runner log found."
    return lines[-1] if lines else "Runner log is empty."


def queue_status(config: dict[str, Any], support_dir: Path, now: float) -> dict[str, Any]:
    app_queue = load_json(support_dir / "stale_queue.json", [])
    process_queue = load_json(support_dir / "process_queue.json", [])
    app_keeps = load_json(support_dir / "custom_whitelist.json", {})
    process_keeps = load_json(support_dir / "process_whitelist.json", {})
    suggestion_state = load_json(support_dir / "state.json", {})
    app_queue = app_queue if isinstance(app_queue, list) else []
    process_queue = process_queue if isinstance(process_queue, list) else []
    app_keeps = app_keeps if isinstance(app_keeps, dict) else {}
    process_keeps = process_keeps if isinstance(process_keeps, dict) else {}

    def snoozed(items: list[dict[str, Any]], hours: float) -> int:
        window = max(0.0, float(hours)) * 3600
        return sum(
            1
            for item in items
            if 0 < float(item.get("last_prompted", 0) or 0)
            and now - float(item.get("last_prompted", 0)) < window
        )

    return {
        "apps": {
            "queued": len(app_queue),
            "snoozed": snoozed(app_queue, config.get("app_snooze_hours", 720)),
            "backed_off": sum(keep_entry_is_active(config, entry, now=now) for entry in app_keeps.values()),
        },
        "processes": {
            "queued": len(process_queue),
            "snoozed": snoozed(process_queue, config.get("process_snooze_hours", 24)),
            "backed_off": sum(
                keep_entry_is_active(config, entry, "process_", now=now)
                for entry in process_keeps.values()
            ),
        },
        "terminal_disabled": len(suggestion_state.get("disabled", {}))
        if isinstance(suggestion_state, dict)
        else 0,
    }


def resource_monitor_status(
    support_dir: Path,
    launch: dict[str, Any],
    now: float,
    recent_limit: int = 5,
) -> dict[str, Any]:
    state = load_json(support_dir / MONITOR_STATE, {})
    state = state if isinstance(state, dict) else {}
    health = state.get("health") if isinstance(state.get("health"), dict) else {}
    return_health = state.get("return_health") if isinstance(state.get("return_health"), dict) else {}
    last_sample = float(health.get("last_sample_at", 0) or 0)
    interval = max(1.0, float(health.get("sample_interval_seconds", 10) or 10))
    stale_after = max(45.0, interval * 3)
    last_error = str(health.get("last_error") or "")
    last_prompt_error = str(health.get("last_prompt_error") or "")
    last_return_error = str(return_health.get("last_error") or health.get("last_return_error") or "")
    last_return_fallback = bool(return_health.get("fallback"))
    last_idle_sample_error = str(health.get("last_idle_sample_error") or "")
    if not state:
        state_status = "not-started"
    elif last_error or last_prompt_error or last_return_error or last_return_fallback or last_idle_sample_error:
        state_status = "degraded"
    elif not last_sample or now - last_sample > stale_after:
        state_status = "stale"
    else:
        state_status = "healthy"
    incidents = state.get("incidents") if isinstance(state.get("incidents"), list) else []
    incidents = sorted(
        [item for item in incidents if isinstance(item, dict)],
        key=lambda item: float(item.get("last_seen_at", item.get("started_at", 0)) or 0),
        reverse=True,
    )[:recent_limit]
    pending = state.get("pending_prompts") if isinstance(state.get("pending_prompts"), list) else []
    active = state.get("active") if isinstance(state.get("active"), dict) else {}
    history = load_recent_jsonl(support_dir / MONITOR_HISTORY, recent_limit)
    healthy = launch.get("healthy", False) and state_status == "healthy"
    return {
        "launchd": launch,
        "state": state_status,
        "healthy": healthy,
        "last_sample_at": last_sample or None,
        "sample_age_seconds": round(now - last_sample, 1) if last_sample else None,
        "last_system_mib_s": health.get("last_system_mib_s"),
        "last_error": last_error,
        "last_prompt_error": last_prompt_error,
        "idle_sample_available": bool(health.get("idle_sample_available")),
        "idle_sample_seconds": health.get("idle_sample_seconds"),
        "last_idle_sample_at": health.get("last_idle_sample_at"),
        "last_idle_sample_error": last_idle_sample_error,
        "return_routing_enabled": bool(health.get("return_routing_enabled", True)),
        "return_active_cutoff_seconds": health.get("return_active_cutoff_seconds", 30),
        "last_return_flow_at": state.get("last_return_flow_at") or health.get("last_return_flow_at"),
        "last_return_success_at": return_health.get("last_success_at") or health.get("last_return_success_at"),
        "last_return_error": last_return_error,
        "last_return_error_at": return_health.get("last_error_at") or health.get("last_return_error_at"),
        "last_return_fallback": last_return_fallback,
        "active_incidents": len(active),
        "pending_prompts": len(pending),
        "sampled_processes": int(health.get("sampled_processes", 0) or 0),
        "tracked_windows": int(health.get("tracked_windows", 0) or 0),
        "recent_incidents": incidents,
        "recent_history": history,
        "attribution_boundary": ATTRIBUTION_NOTE,
    }


def render_text(status: dict[str, Any]) -> str:
    runner = status["runner"]
    monitor = status["resource_monitor"]
    queues = status["queues"]
    terminal = status["terminal_suggestions"]
    intelligence = status.get("pattern_intelligence", {})
    monitor_launch = monitor["launchd"]
    if not monitor["return_routing_enabled"]:
        return_summary = "disabled by configuration"
    elif monitor["last_return_error"]:
        return_summary = f"error: {monitor['last_return_error']}"
    elif monitor["last_return_fallback"]:
        return_summary = "fallback used on last return"
    elif monitor["last_return_success_at"]:
        return_summary = "coordinator completed on last return"
    else:
        return_summary = "waiting for first away-return"
    lines = [
        "Idle maintenance status",
        "",
        f"Scheduled runner: {runner['summary']}",
        f"Launchd state: {runner['state']}",
        f"Last exit code: {runner['last_exit_code'] if runner['last_exit_code'] is not None else 'unknown'}",
        f"Latest event: {status['latest_event']}",
        "",
        "Resource monitor:",
        f"- Health: {monitor['state']} (launchd: {monitor_launch['state']})",
        f"- Active incidents: {monitor['active_incidents']}; queued review prompts: {monitor['pending_prompts']}",
        f"- Current sampling: {monitor['sampled_processes']} processes; {monitor['tracked_windows']} candidate windows",
        f"- Latest system sample: {monitor['last_system_mib_s'] if monitor['last_system_mib_s'] is not None else 'unavailable'} MiB/s",
        (
            f"- HID idle sample: {monitor['idle_sample_seconds']:.1f}s"
            if monitor["idle_sample_available"] and monitor["idle_sample_seconds"] is not None
            else f"- HID idle sample: unavailable ({monitor['last_idle_sample_error'] or 'not sampled yet'})"
        ),
        f"- Resume routing: {return_summary}",
        f"- Attribution: {monitor['attribution_boundary']}",
        "",
        "Interactive review:",
        f"- Apps: {queues['apps']['queued']} queued, {queues['apps']['snoozed']} snoozed, {queues['apps']['backed_off']} backed off",
        f"- Live processes: {queues['processes']['queued']} queued, {queues['processes']['snoozed']} snoozed, {queues['processes']['backed_off']} backed off",
        f"- Terminal: {'available now' if terminal['available_now'] else 'quiet now'} ({terminal['window']}), {queues['terminal_disabled']} dismissed",
    ]
    for incident in monitor["recent_incidents"][:3]:
        lines.append(
            f"- Incident {incident.get('process', 'process')} PID {incident.get('pid', '?')}: "
            f"{incident.get('status', 'unknown')}, peak {float(incident.get('peak_total_mib_s', 0)):.1f} MiB/s"
        )
    latest_diagnosis = intelligence.get("latest") if isinstance(intelligence, dict) else None
    health = intelligence.get("health") if isinstance(intelligence, dict) else {}
    lines.extend(
        [
            "",
            "Pattern intelligence:",
            f"- Vector backend: {health.get('vector_backend') or 'not-started'}; stored events: {intelligence.get('events', 0)}; pending spikes: {intelligence.get('pending_spikes', 0)}",
            f"- Worker error: {health.get('last_error') or health.get('embedding_error') or 'none'}",
        ]
    )
    if isinstance(latest_diagnosis, dict):
        structured = latest_diagnosis.get("diagnosis") if isinstance(latest_diagnosis.get("diagnosis"), dict) else {}
        confidence = structured.get("confidence")
        lines.append(
            f"- Latest remedy{f' ({float(confidence):.0%} confidence)' if confidence is not None else ''}: "
            f"{' '.join(str(latest_diagnosis.get('response') or '').split())[:300]}"
        )
    runner_output = runner.get("status_output", "").strip()
    if runner_output:
        lines.extend(["", "Runner details:", runner_output])
    elif runner.get("status_error"):
        lines.extend(["", f"Runner details unavailable: {runner['status_error']}"])
    return "\n".join(lines)


def collect_status(config=None, command_runner=subprocess.run, home=None, now=None):
    home = Path(home or Path.home())
    now = time.time() if now is None else float(now)
    config = config or load_config(os.path.dirname(__file__))
    support_dir = home / "Library/Application Support/idle-maintenance"
    log_path = home / "Library/Logs/wiki-automation/idle-maintenance-runtime.log"

    launch_status = launchd_status(LAUNCHD_LABEL, command_runner)
    monitor_launch = launchd_status(MONITOR_LABEL, command_runner)
    runner_command = resolve_runner_command(config, home)
    status_output = ""
    status_error = ""
    if runner_command:
        try:
            result = command_runner(runner_command, capture_output=True, text=True, timeout=10)
            status_output = result.stdout.strip()
            if result.returncode != 0:
                status_error = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            status_error = str(error)
    else:
        status_error = "No runner command configured or discovered."

    try:
        start = int(config.get("terminal_suggestion_start_hour", 9))
        end = int(config.get("terminal_suggestion_end_hour", 21))
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError
    except (TypeError, ValueError):
        start, end = 9, 21
    try:
        from activity_intelligence import status as intelligence_status

        pattern_intelligence = intelligence_status(support_dir / "activity-intelligence.sqlite3")
    except Exception as error:
        pattern_intelligence = {
            "events": 0,
            "stored_diagnoses": 0,
            "pending_spikes": 0,
            "latest": None,
            "health": {"last_error": str(error)[:500], "vector_backend": "unavailable"},
        }
    status = {
        "runner": {
            **launch_status,
            "status_command_available": bool(runner_command),
            "status_output": status_output,
            "status_error": status_error,
        },
        "resource_monitor": resource_monitor_status(support_dir, monitor_launch, now),
        "pattern_intelligence": pattern_intelligence,
        "latest_event": latest_log_event(log_path),
        "queues": queue_status(config, support_dir, now),
        "terminal_suggestions": {
            "available_now": is_terminal_suggestion_time(config, datetime.fromtimestamp(now)),
            "window": "always" if start == end else f"{start:02d}:00–{end:02d}:00",
        },
    }
    status["text"] = render_text(status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    status = collect_status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(status["text"])
    monitor_started = status["resource_monitor"]["state"] != "not-started"
    healthy = status["runner"]["healthy"] and (
        not monitor_started or status["resource_monitor"]["healthy"]
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
