#!/usr/bin/env python3
"""Refresh shortcut-review content before opening the GUI review surface."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

RunFn = Callable[..., subprocess.CompletedProcess[str]]


def normalize_command(value: Any, home: Path | None = None) -> list[str]:
    """Return a rootless argv list with environment and home expansion."""
    if isinstance(value, str):
        try:
            value = shlex.split(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    root = Path(home or Path.home())
    command: list[str] = []
    for raw in value:
        part = os.path.expandvars(str(raw))
        if part == "~" or part.startswith("~/"):
            part = str(root) + part[1:]
        command.append(part)
    return command


def _step_result(name: str, command: list[str], result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "name": name,
        "command": command,
        "returncode": int(result.returncode),
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def run_shortcut_review(
    config: dict[str, Any] | None = None,
    *,
    runner: RunFn = subprocess.run,
    home: Path | None = None,
) -> dict[str, Any]:
    """Refresh focused review content, then open the popup only after success."""
    if config is None:
        from idle_config import load_config

        config = load_config(os.path.dirname(__file__))

    refresh = normalize_command(config.get("return_flashcard_refresh_command"), home)
    popup = normalize_command(
        config.get("return_shortcut_popup_command") or config.get("shortcut_review_command"),
        home,
    )
    if not refresh:
        return {
            "ok": False,
            "failed_step": "refresh",
            "error": "No shortcut content refresh command is configured.",
            "steps": [],
        }
    if not popup:
        return {
            "ok": False,
            "failed_step": "popup",
            "error": "No shortcut review popup command is configured.",
            "steps": [],
        }

    steps: list[dict[str, Any]] = []
    for name, command in (("refresh", refresh), ("popup", popup)):
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            steps.append(
                {
                    "name": name,
                    "command": command,
                    "returncode": 126,
                    "stdout": "",
                    "stderr": str(error),
                }
            )
            return {
                "ok": False,
                "failed_step": name,
                "error": str(error),
                "steps": steps,
            }
        step = _step_result(name, command, completed)
        steps.append(step)
        if completed.returncode != 0:
            detail = step["stderr"] or step["stdout"] or f"exit {completed.returncode}"
            return {
                "ok": False,
                "failed_step": name,
                "error": detail,
                "steps": steps,
            }

    return {"ok": True, "failed_step": None, "error": "", "steps": steps}


def render_result(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "Shortcut content refreshed; review popup opened."
    step = str(result.get("failed_step") or "review")
    detail = str(result.get("error") or "unknown error")
    if step == "refresh":
        return f"Shortcut content refresh failed; review popup was not opened: {detail}"
    return f"Shortcut review popup failed: {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    result = run_shortcut_review()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_result(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
