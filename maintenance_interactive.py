"""Compatibility entrypoint with resource-aware process review installed."""
from __future__ import annotations

import os
import sys

import activity_intelligence as _activity_intelligence
import maintenance_core as _core
import process_review as _process_review
from idle_config import load_config
from process_review import install as _install
from prompt_session import close_review_session
from review_ui import install as _install_review_ui
from shortcut_review import render_result, run_shortcut_review

_install(_core)
_install_review_ui(_core, _process_review)
_activity_intelligence.install_codex_event_hook(_core)
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def _finish_shortcut_review() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--process-audit":
        return
    if os.environ.get("IDLE_MAINTENANCE_SKIP_SHORTCUT_REVIEW") == "1":
        return
    result = run_shortcut_review(load_config(_core.BASE_DIR))
    if not result.get("ok"):
        print(render_result(result), file=sys.stderr)


def _start_activity_intelligence() -> None:
    """Process accumulated activity evidence without blocking the interactive handoff."""
    config = load_config(_core.BASE_DIR)
    if not _activity_intelligence.launch_cycle(config, base_dir=_core.BASE_DIR):
        _core.log("Activity intelligence cycle was not launched (disabled or unavailable).")


if __name__ == "__main__":
    try:
        _result = _core.main()
    finally:
        close_review_session(_core.BASE_DIR)
    _finish_shortcut_review()
    _start_activity_intelligence()
    raise SystemExit(_result)
