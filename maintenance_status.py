#!/usr/bin/env python3
"""Report scheduled and interactive idle-maintenance status."""

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from idle_config import is_terminal_suggestion_time, keep_entry_is_active, load_config

LAUNCHD_LABEL = "com.john.idle-maintenance"


def load_json(path, default):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def expand_command(command, home):
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


def resolve_runner_command(config, home):
    configured = expand_command(config.get("scheduled_runner_status_command", []), home)
    if configured:
        return configured
    wiki_root = os.environ.get("WIKI_PATH") or os.environ.get("WIKI_ROOT")
    root = Path(wiki_root).expanduser() if wiki_root else home / "wiki"
    runner = root / "99_meta/scripts/idle_maintenance_runner.sh"
    return [str(runner), "--status"] if runner.is_file() else []


def parse_launchctl(output, returncode):
    state_match = re.search(r"^\s*state = (.+)$", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)$", output, re.MULTILINE)
    loaded = returncode == 0
    last_exit = int(exit_match.group(1)) if exit_match else None
    state = state_match.group(1).strip() if state_match else ("unloaded" if not loaded else "unknown")
    healthy = loaded and (last_exit in {None, 0})
    return {
        "loaded": loaded,
        "state": state,
        "last_exit_code": last_exit,
        "healthy": healthy,
        "summary": (
            "Loaded and healthy (idle between scheduled runs)"
            if healthy and state == "not running"
            else "Loaded and healthy" if healthy
            else "Needs attention"
        ),
    }


def latest_log_event(log_path):
    try:
        lines = [line.strip() for line in log_path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "No runner log found."
    return lines[-1] if lines else "Runner log is empty."


def queue_status(config, support_dir, now):
    app_queue = load_json(support_dir / "stale_queue.json", [])
    process_queue = load_json(support_dir / "process_queue.json", [])
    app_keeps = load_json(support_dir / "custom_whitelist.json", {})
    process_keeps = load_json(support_dir / "process_whitelist.json", {})
    suggestion_state = load_json(support_dir / "state.json", {})
    if not isinstance(app_queue, list):
        app_queue = []
    if not isinstance(process_queue, list):
        process_queue = []
    if not isinstance(app_keeps, dict):
        app_keeps = {}
    if not isinstance(process_keeps, dict):
        process_keeps = {}

    def snoozed(items, hours):
        window = max(0.0, float(hours)) * 3600
        return sum(
            1 for item in items
            if 0 < float(item.get("last_prompted", 0) or 0)
            and now - float(item.get("last_prompted", 0)) < window
        )

    return {
        "apps": {
            "queued": len(app_queue),
            "snoozed": snoozed(app_queue, config.get("app_snooze_hours", 720)),
            "backed_off": sum(
                keep_entry_is_active(config, entry, now=now) for entry in app_keeps.values()
            ),
        },
        "processes": {
            "queued": len(process_queue),
            "snoozed": snoozed(process_queue, config.get("process_snooze_hours", 24)),
            "backed_off": sum(
                keep_entry_is_active(config, entry, "process_", now=now)
                for entry in process_keeps.values()
            ),
        },
        "terminal_disabled": len(suggestion_state.get("disabled", {})),
    }


def render_text(status):
    runner = status["runner"]
    queues = status["queues"]
    terminal = status["terminal_suggestions"]
    lines = [
        "Idle maintenance status",
        "",
        f"Scheduled runner: {runner['summary']}",
        f"Launchd state: {runner['state']}",
        f"Last exit code: {runner['last_exit_code'] if runner['last_exit_code'] is not None else 'unknown'}",
        f"Latest event: {status['latest_event']}",
        "",
        "Interactive review:",
        f"- Apps: {queues['apps']['queued']} queued, {queues['apps']['snoozed']} snoozed, {queues['apps']['backed_off']} backed off",
        f"- Processes: {queues['processes']['queued']} queued, {queues['processes']['snoozed']} snoozed, {queues['processes']['backed_off']} backed off",
        f"- Terminal: {'available now' if terminal['available_now'] else 'quiet now'} ({terminal['window']}), {queues['terminal_disabled']} dismissed",
    ]
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

    launch = command_runner(
        ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    launch_status = parse_launchctl(launch.stdout or launch.stderr, launch.returncode)

    runner_command = resolve_runner_command(config, home)
    status_output = ""
    status_error = ""
    if runner_command:
        try:
            result = command_runner(
                runner_command, capture_output=True, text=True, timeout=10
            )
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
    status = {
        "runner": {
            **launch_status,
            "status_command_available": bool(runner_command),
            "status_output": status_output,
            "status_error": status_error,
        },
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
    return 0 if status["runner"]["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
