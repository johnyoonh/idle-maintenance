"""Compatibility entrypoint with resource-aware process review installed."""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import time
from typing import Any, Callable, Iterator

import activity_intelligence as _activity_intelligence
import app_actions as _app_actions
import maintenance_core as _core
import process_review as _process_review
from idle_config import keep_entry_is_active, load_config, next_keep_delay_days
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


@contextlib.contextmanager
def _interactive_lock() -> Iterator[bool]:
    """Use the existing interactive lock without involving the detached worker lock."""
    os.makedirs(os.path.dirname(_core.LOCK_FILE), exist_ok=True)
    handle = open(_core.LOCK_FILE, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _persist_app_state(current_queue: list[dict[str, Any]], whitelist: dict[str, Any]) -> None:
    """Persist each review disposition before another prompt can be shown."""
    _core.save_json(_core.QUEUE_PATH, current_queue)
    _core.save_json(_core.WHITELIST_PATH, whitelist)


def _remove_app(current_queue: list[dict[str, Any]], app_path: str) -> list[dict[str, Any]]:
    return [entry for entry in current_queue if entry.get("path") != app_path]


def _handle_app_action(
    action: str,
    item: dict[str, Any],
    current_queue: list[dict[str, Any]],
    whitelist: dict[str, Any],
    config: dict[str, Any],
    *,
    now_fn: Callable[[], float] = time.time,
    enqueue_fn: Callable[..., dict[str, Any]] | None = None,
    launch_fn: Callable[..., bool] | None = None,
    open_runner: Callable[..., Any] = subprocess.run,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Apply one app decision, persisting it before the caller advances."""
    app_path = str(item["path"])
    if action in {"KEEP", "WHITELIST"}:
        _core.record_keep(whitelist, app_path)
        current_queue = _remove_app(current_queue, app_path)
        _persist_app_state(current_queue, whitelist)
        return current_queue, True, 1

    if action == "DELETE":
        enqueue = enqueue_fn or _app_actions.enqueue_trash_action
        launch = launch_fn or _app_actions.launch_worker
        try:
            enqueue(app_path)
        except Exception as error:
            _core.log(f"Failed to queue Trash action for {app_path}: {error}")
            _core.notify_user(
                "Idle Maintenance",
                f"Could not queue {os.path.basename(app_path)} for Trash; the review remains open.",
            )
            return current_queue, False, 0

        current_queue = _remove_app(current_queue, app_path)
        _persist_app_state(current_queue, whitelist)
        if not launch(base_dir=_core.BASE_DIR):
            _core.log(
                f"Trash action for {app_path} is durable, but the background worker could not start; "
                "it will resume on a later maintenance run."
            )
            _core.notify_user(
                "Idle Maintenance",
                f"Queued {os.path.basename(app_path)} for Trash; background processing will resume later.",
            )
        return current_queue, True, 1

    if action == "TRY":
        # Compatibility only. Updated prompt.swift handles Open itself and emits no disposition.
        try:
            open_runner(["open", app_path], check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        return current_queue, False, 0

    # SNOOZE/SKIP remain cheap interactive state changes.
    for entry in current_queue:
        if entry.get("path") == app_path:
            entry["last_prompted"] = int(now_fn())
    _persist_app_state(current_queue, whitelist)
    return current_queue, True, 1


def _run_app_review() -> Any:
    """Run the app review stage while destructive work continues in a detached worker."""
    with _interactive_lock() as acquired:
        if not acquired:
            _core.log("Already running (interactive lock active). Exiting.")
            return None

        config = load_config(_core.BASE_DIR)
        # Resume previously queued work on every interactive launch. A singleton worker
        # lock keeps this safe even if a worker is already active.
        _app_actions.launch_worker(base_dir=_core.BASE_DIR)

        max_entries = int(config.get("max_entries_per_idle_return", config.get("max_prompts", _core.DEFAULT_MAX_PROMPTS)))
        max_entries = max(0, max_entries)

        process_ok, process_prompts = _core.run_process_audit(config, prompt_budget=max_entries)
        if not process_ok:
            return None
        remaining_prompts = max(0, max_entries - process_prompts)

        auditor_path = os.path.join(_core.BASE_DIR, "app_auditor.py")
        try:
            stale_output = subprocess.check_output(["/usr/bin/python3", auditor_path], text=True).splitlines()
        except (OSError, subprocess.SubprocessError):
            stale_output = []

        stale_apps: list[str] = []
        stale_dates: dict[str, str] = {}
        for line in stale_output:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                path, date_str = line.split("|", 1)
                stale_apps.append(path)
                stale_dates[path] = date_str
            else:
                stale_apps.append(line)
                stale_dates[line] = "Unknown"

        # A durable pending/running action is already a final decision for this audit.
        # Do not surface the same app again while that action is still active.
        active_action_paths = _app_actions.active_action_paths()
        stale_apps = [path for path in stale_apps if path not in active_action_paths]

        max_prompts = min(int(config.get("max_prompts", _core.DEFAULT_MAX_PROMPTS)), remaining_prompts)
        close_on_unfocus = False
        app_snooze_hours = max(0.0, float(config.get("app_snooze_hours", 720)))
        stale_days_limit = int(config.get("stale_days_limit", 90))

        queue = _core.load_json(_core.QUEUE_PATH)
        if not isinstance(queue, list):
            queue = []
        whitelist = _core.load_custom_whitelist(_core.WHITELIST_PATH)

        queue = [item for item in queue if isinstance(item, dict) and item.get("path") in stale_apps]
        existing_paths = {item.get("path") for item in queue}
        for app in stale_apps:
            if app not in existing_paths and not keep_entry_is_active(config, whitelist.get(app)):
                queue.append({"path": app, "last_prompted": 0})

        queue.sort(key=lambda item: item.get("last_prompted", 0))
        processed = 0
        current_queue = [dict(item) for item in queue]

        for item in queue:
            if processed >= max_prompts:
                break
            if _core.queue_item_is_snoozed(item, app_snooze_hours):
                continue

            app_done = False
            while not app_done and processed < max_prompts:
                app_path = str(item["path"])
                last_used_info = _core.app_usage_detail(
                    stale_dates.get(app_path, "Unknown"), stale_days_limit
                )
                restore_source = _core.get_restore_source(config, app_path)
                cleanup, _ = _core.app_cleanup_config(config)
                allow_unknown_restore = bool(cleanup.get("allow_unknown_restore_source", False))
                if restore_source.get("source") == "unknown" and not allow_unknown_restore:
                    last_used_info += " • Restore: unknown; delete disabled"
                elif restore_source.get("source") == "unknown":
                    last_used_info += " • Restore: unknown"
                else:
                    last_used_info += f" • Restore: {restore_source.get('restore_command', restore_source.get('source'))}"
                if item.get("last_prompted", 0) > 0:
                    last_used_info += (
                        " (Last prompted/tried: "
                        + time.strftime("%Y-%m-%d", time.localtime(item["last_prompted"]))
                        + ")"
                    )
                keep_days = next_keep_delay_days(config, whitelist.get(app_path))
                action = _core.prompt_user(
                    app_path,
                    close_on_unfocus,
                    last_used_info,
                    snooze_hours=app_snooze_hours,
                    keep_days=keep_days,
                )

                if action == "QUIT":
                    _persist_app_state(current_queue, whitelist)
                    return None

                current_queue, app_done, delta = _handle_app_action(
                    action,
                    item,
                    current_queue,
                    whitelist,
                    config,
                )
                processed += delta

        _persist_app_state(current_queue, whitelist)
    return None


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
        if len(sys.argv) > 1 and sys.argv[1] == "--process-audit":
            _result = _core.main()
        else:
            _result = _run_app_review()
    finally:
        close_review_session(_core.BASE_DIR)
    _finish_shortcut_review()
    _start_activity_intelligence()
    raise SystemExit(_result)
