#!/usr/bin/env python3
"""Rotate keyboard and Apple Shortcut review providers."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

RunFn = Callable[..., subprocess.CompletedProcess[str]]
PROVIDERS = ("keyboard", "apple")


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


def _default_state_path(home: Path) -> Path:
    return home / "Library/Application Support/idle-maintenance/shortcut-review-providers.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _provider_commands(config: dict[str, Any], provider: str, home: Path) -> tuple[list[str], list[str]]:
    if provider == "keyboard":
        return (
            normalize_command(config.get("return_flashcard_refresh_command"), home),
            normalize_command(
                config.get("return_shortcut_popup_command") or config.get("shortcut_review_command"),
                home,
            ),
        )
    return (
        normalize_command(config.get("apple_shortcut_review_refresh_command"), home),
        normalize_command(config.get("apple_shortcut_review_popup_command"), home),
    )


def _last_shown(state: dict[str, Any], provider: str) -> str:
    return str(state.get("providers", {}).get(provider, {}).get("lastShownAt", ""))


def _run_provider(
    provider: str,
    config: dict[str, Any],
    *,
    runner: RunFn,
    home: Path,
    require_candidates: bool,
) -> dict[str, Any]:
    refresh, popup = _provider_commands(config, provider, home)
    if not refresh:
        return {"ok": False, "provider": provider, "failed_step": "refresh", "error": f"No {provider} refresh command is configured.", "steps": []}
    if not popup:
        return {"ok": False, "provider": provider, "failed_step": "popup", "error": f"No {provider} review command is configured.", "steps": []}

    steps: list[dict[str, Any]] = []
    for name, command in (("refresh", refresh), ("popup", popup)):
        try:
            completed = runner(command, capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            steps.append({"name": name, "command": command, "returncode": 126, "stdout": "", "stderr": str(error)})
            return {"ok": False, "provider": provider, "failed_step": name, "error": str(error), "steps": steps}
        step = _step_result(name, command, completed)
        steps.append(step)
        if completed.returncode != 0:
            detail = step["stderr"] or step["stdout"] or f"exit {completed.returncode}"
            return {"ok": False, "provider": provider, "failed_step": name, "error": detail, "steps": steps}
        if provider == "apple" and name == "refresh" and require_candidates:
            try:
                actionable = int(json.loads(step["stdout"]).get("actionableCount", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"ok": False, "provider": provider, "failed_step": "refresh", "error": "Apple review refresh did not return valid JSON.", "steps": steps}
            if actionable == 0:
                return {"ok": True, "provider": provider, "skipped": True, "reason": "no-candidates", "failed_step": None, "error": "", "steps": steps}

    return {"ok": True, "provider": provider, "skipped": False, "failed_step": None, "error": "", "steps": steps}


def run_shortcut_review(
    config: dict[str, Any] | None = None,
    *,
    provider: str | None = None,
    automatic: bool = False,
    runner: RunFn = subprocess.run,
    home: Path | None = None,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Open an explicit provider or the least-recently successful provider."""
    if config is None:
        from idle_config import load_config

        config = load_config(os.path.dirname(__file__))
    if provider not in (None, *PROVIDERS):
        return {"ok": False, "provider": provider, "failed_step": "provider", "error": f"Unknown shortcut review provider: {provider}", "steps": []}

    root = Path(home or Path.home())
    timestamp = now or datetime.now().astimezone()
    path = Path(state_path or config.get("shortcut_review_state_path") or _default_state_path(root))
    state = _load_state(path)
    today = timestamp.astimezone().date().isoformat()
    if automatic and state.get("lastAutomaticReviewDate") == today:
        return {"ok": True, "provider": None, "skipped": True, "reason": "already-reviewed-today", "failed_step": None, "error": "", "steps": []}

    explicit = provider is not None
    order = [provider] if explicit else sorted(PROVIDERS, key=lambda name: (_last_shown(state, name), PROVIDERS.index(name)))
    attempts: list[dict[str, Any]] = []
    for candidate in order:
        result = _run_provider(candidate, config, runner=runner, home=root, require_candidates=not explicit)
        attempts.append(result)
        if result.get("ok") and not result.get("skipped"):
            state.setdefault("providers", {}).setdefault(candidate, {})["lastShownAt"] = timestamp.isoformat()
            if automatic:
                state["lastAutomaticReviewDate"] = today
            _save_state(path, state)
            result["attempts"] = attempts
            return result
        if explicit:
            result["attempts"] = attempts
            return result

    errors = [item.get("error") for item in attempts if not item.get("ok") and item.get("error")]
    if not errors and attempts:
        return {"ok": True, "provider": None, "skipped": True, "reason": "no-candidates", "failed_step": None, "error": "", "steps": [], "attempts": attempts}
    return {"ok": False, "provider": None, "failed_step": "providers", "error": "; ".join(errors) or "No review provider was available.", "steps": [], "attempts": attempts}


def render_result(result: dict[str, Any]) -> str:
    if result.get("skipped"):
        if result.get("reason") == "already-reviewed-today":
            return "Shortcut review already opened today."
        return "No Apple Shortcut candidates were due for review."
    if result.get("ok"):
        return f"{str(result.get('provider', 'shortcut')).title()} shortcut review opened."
    step = str(result.get("failed_step") or "review")
    detail = str(result.get("error") or "unknown error")
    if step == "refresh":
        return f"Shortcut content refresh failed; review popup was not opened: {detail}"
    return f"Shortcut review failed: {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--automatic", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    result = run_shortcut_review(provider=args.provider, automatic=args.automatic)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_result(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
