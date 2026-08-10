#!/usr/bin/env python3
"""Extend maintenance status with the optional away-return review watcher."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import maintenance_status as base
from shortcut_review import normalize_command

WATCHER_PATTERN = "[i]dle_watcher.py"


def away_return_review_status(
    config: dict[str, Any],
    *,
    command_runner=subprocess.run,
    home: Path | None = None,
) -> dict[str, Any]:
    root = Path(home or Path.home())
    try:
        result = command_runner(
            ["/usr/bin/pgrep", "-f", WATCHER_PATTERN],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        running = result.returncode == 0 and bool((result.stdout or "").strip())
        error = "" if result.returncode in {0, 1} else (result.stderr or result.stdout or "pgrep failed").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        running = False
        error = str(exc)

    idle_seconds = max(0.0, float(config.get("idle_threshold_minutes", 10))) * 60
    cooldown_seconds = max(0.0, float(config.get("post_trigger_cooldown_seconds", 3600)))
    focus = normalize_command(
        config.get("return_focus_command") or config.get("return_handoff_command"),
        root,
    )
    return {
        "optional": True,
        "running": running,
        "summary": "Running" if running else "Not running",
        "error": error,
        "idle_threshold_seconds": idle_seconds,
        "return_idle_below_seconds": 30,
        "cooldown_seconds": cooldown_seconds,
        "resume_focus_configured": bool(focus),
        "resume_focus_command": focus,
    }


def render_text(status: dict[str, Any]) -> str:
    watcher = status["away_return_review"]
    section = [
        "Away-return review (optional):",
        f"- State: {watcher['summary']}",
        (
            f"- Trigger: idle over {watcher['idle_threshold_seconds'] / 60:g} min, "
            f"return below {watcher['return_idle_below_seconds']:g}s, "
            f"cooldown {watcher['cooldown_seconds'] / 60:g} min"
        ),
        (
            "- Resume focus: Hammerspoon context router configured"
            if watcher["resume_focus_configured"]
            else "- Resume focus: no coordinator command configured; legacy fallback only"
        ),
    ]
    if watcher.get("error"):
        section.append(f"- Status error: {watcher['error']}")

    base_text = str(status.get("text") or "").rstrip()
    marker = "\n\nRunner details:"
    if marker in base_text:
        before, after = base_text.split(marker, 1)
        return before + "\n\n" + "\n".join(section) + marker + after
    return base_text + "\n\n" + "\n".join(section)


def collect_status(
    config: dict[str, Any] | None = None,
    *,
    command_runner=subprocess.run,
    home: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    root = Path(home or Path.home())
    current = time.time() if now is None else float(now)
    if config is None:
        config = base.load_config(str(Path(__file__).resolve().parent))
    status = base.collect_status(
        config=config,
        command_runner=command_runner,
        home=root,
        now=current,
    )
    status["away_return_review"] = away_return_review_status(
        config,
        command_runner=command_runner,
        home=root,
    )
    status["text"] = render_text(status)
    return status


def _healthy(status: dict[str, Any]) -> bool:
    monitor_started = status["resource_monitor"]["state"] != "not-started"
    return bool(
        status["runner"]["healthy"]
        and (not monitor_started or status["resource_monitor"]["healthy"])
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    status = collect_status()
    print(json.dumps(status, indent=2, sort_keys=True) if args.json else status["text"])
    return 0 if _healthy(status) else 1


if __name__ == "__main__":
    raise SystemExit(main())
