#!/usr/bin/env python3
"""Durable serial background processing for destructive application actions."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from idle_config import APP_SUPPORT_DIR, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(APP_SUPPORT_DIR, "app-actions.json")
STATE_LOCK_PATH = os.path.join(APP_SUPPORT_DIR, "app-actions-state.lock")
WORKER_LOCK_PATH = os.path.join(APP_SUPPORT_DIR, "app-actions-worker.lock")
TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_TERMINAL_JOBS = 100
ACTIVE_STATES = {"pending", "running"}
TERMINAL_STATES = {"completed", "failed"}


def _default_state() -> dict[str, Any]:
    return {"version": 1, "jobs": []}


def _state_lock_path(state_path: str, lock_path: str | None) -> str:
    if lock_path:
        return lock_path
    if os.path.abspath(state_path) == os.path.abspath(STATE_PATH):
        return STATE_LOCK_PATH
    return state_path + ".lock"


def _load_state_unlocked(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return _default_state()
    data.setdefault("version", 1)
    return data


def _atomic_write(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".app-actions.", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prune_jobs(state: dict[str, Any], now: float) -> None:
    active: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    cutoff = now - TERMINAL_RETENTION_SECONDS
    for raw in state.get("jobs", []):
        if not isinstance(raw, dict):
            continue
        job = dict(raw)
        if job.get("state") in ACTIVE_STATES:
            active.append(job)
            continue
        if job.get("state") not in TERMINAL_STATES:
            continue
        finished = float(job.get("finished_at") or 0)
        if finished >= cutoff:
            terminal.append(job)
    terminal.sort(key=lambda job: float(job.get("finished_at") or 0), reverse=True)
    state["jobs"] = active + terminal[:MAX_TERMINAL_JOBS]


@contextlib.contextmanager
def _locked_state(
    state_path: str = STATE_PATH,
    *,
    lock_path: str | None = None,
    now: float | None = None,
) -> Iterator[dict[str, Any]]:
    resolved_lock = _state_lock_path(state_path, lock_path)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    os.makedirs(os.path.dirname(resolved_lock), exist_ok=True)
    current = time.time() if now is None else float(now)
    with open(resolved_lock, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_state_unlocked(state_path)
        _prune_jobs(state, current)
        try:
            yield state
        finally:
            _prune_jobs(state, current)
            _atomic_write(state_path, state)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def enqueue_trash_action(
    app_path: str,
    *,
    state_path: str = STATE_PATH,
    lock_path: str | None = None,
    now: float | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Durably enqueue one Trash request before the review advances."""
    path = os.path.abspath(os.path.expanduser(str(app_path)))
    if not path or path == os.path.sep:
        raise ValueError("invalid application path")
    requested = time.time() if now is None else float(now)
    job = {
        "id": job_id or uuid.uuid4().hex,
        "action": "trash",
        "app_path": path,
        "state": "pending",
        "requested_at": requested,
        "started_at": None,
        "finished_at": None,
        "error": "",
        "result": {},
    }
    with _locked_state(state_path, lock_path=lock_path, now=requested) as state:
        state["jobs"].append(job)
    return dict(job)


def _claim_next_job(
    *,
    state_path: str,
    lock_path: str | None,
    now: float,
) -> dict[str, Any] | None:
    claimed: dict[str, Any] | None = None
    with _locked_state(state_path, lock_path=lock_path, now=now) as state:
        pending = [job for job in state["jobs"] if job.get("state") == "pending"]
        pending.sort(key=lambda job: float(job.get("requested_at") or 0))
        if pending:
            target_id = pending[0].get("id")
            for job in state["jobs"]:
                if job.get("id") == target_id:
                    job["state"] = "running"
                    job["started_at"] = now
                    job["error"] = ""
                    claimed = dict(job)
                    break
    return claimed


def _finish_job(
    job_id: str,
    outcome: dict[str, Any],
    *,
    state_path: str,
    lock_path: str | None,
    now: float,
) -> None:
    terminal_state = "completed" if outcome.get("state") == "completed" else "failed"
    with _locked_state(state_path, lock_path=lock_path, now=now) as state:
        for job in state["jobs"]:
            if job.get("id") != job_id:
                continue
            job["state"] = terminal_state
            job["finished_at"] = now
            job["error"] = str(outcome.get("error") or "")
            result = outcome.get("result")
            job["result"] = dict(result) if isinstance(result, dict) else {}
            break


def _fail_interrupted_running(
    *,
    state_path: str,
    lock_path: str | None,
    now: float,
) -> None:
    """Do not retry an interrupted destructive action automatically."""
    with _locked_state(state_path, lock_path=lock_path, now=now) as state:
        for job in state["jobs"]:
            if job.get("state") != "running":
                continue
            job["state"] = "failed"
            job["finished_at"] = now
            job["error"] = "worker interrupted before completion status was recorded; not retried"
            job["result"] = {"outcome": "interrupted"}


def _execute_trash_job(job: dict[str, Any], *, base_dir: str = BASE_DIR) -> dict[str, Any]:
    """Revalidate current policy and delegate the actual move/ledger/hooks to maintenance_core."""
    import maintenance_core as core

    app_path = os.path.abspath(os.path.expanduser(str(job.get("app_path") or "")))
    app_name = os.path.basename(app_path) or "application"
    if not app_path or app_path == os.path.sep or not app_path.endswith(".app"):
        core.notify_user("Idle Maintenance", f"Trash action failed for {app_name}: invalid application path.")
        return {"state": "failed", "error": "invalid application path", "result": {"outcome": "invalid-path"}}
    if not os.path.exists(app_path):
        core.notify_user("Idle Maintenance", f"Skipped {app_name}: application is no longer installed.")
        return {"state": "completed", "result": {"outcome": "missing-app"}}

    try:
        config = load_config(base_dir)
        cleanup, hooks = core.app_cleanup_config(config)
        restore_source = core.get_restore_source(config, app_path)
        # Touch the values here so the worker explicitly validates their current shape;
        # maintenance_core.delete_app re-reads and enforces them immediately before moving.
        if not isinstance(cleanup, dict) or not isinstance(hooks, dict) or not isinstance(restore_source, dict):
            raise RuntimeError("invalid cleanup policy state")
        success = bool(core.delete_app(app_path, config))
    except Exception as error:
        core.log(f"Background Trash action failed for {app_path}: {error}")
        core.notify_user("Idle Maintenance", f"Could not process Trash action for {app_name}. See IdleMaintenance.log.")
        return {
            "state": "failed",
            "error": str(error),
            "result": {"outcome": "exception"},
        }

    if not success:
        return {
            "state": "failed",
            "error": "destructive action was refused or failed",
            "result": {"outcome": "delete-failed", "restore_source": restore_source},
        }

    restore_command = str(restore_source.get("restore_command") or "")
    restore_note = f" Restore with: {restore_command}" if restore_command else ""
    core.notify_user("Idle Maintenance", f"Moved {app_name} to Trash.{restore_note}")
    return {
        "state": "completed",
        "result": {
            "outcome": "trashed",
            "restore_command": restore_command,
            "restore_source": restore_source,
        },
    }


def run_worker(
    *,
    state_path: str = STATE_PATH,
    state_lock_path: str | None = None,
    worker_lock_path: str = WORKER_LOCK_PATH,
    base_dir: str = BASE_DIR,
    execute: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    now_fn: Callable[[], float] = time.time,
) -> int:
    """Run pending destructive actions strictly one at a time under a singleton lock."""
    os.makedirs(os.path.dirname(worker_lock_path), exist_ok=True)
    with open(worker_lock_path, "a+", encoding="utf-8") as worker_lock:
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        worker_lock.seek(0)
        worker_lock.truncate()
        worker_lock.write(str(os.getpid()))
        worker_lock.flush()

        _fail_interrupted_running(
            state_path=state_path,
            lock_path=state_lock_path,
            now=float(now_fn()),
        )
        executor = execute or (lambda job: _execute_trash_job(job, base_dir=base_dir))
        while True:
            claimed = _claim_next_job(
                state_path=state_path,
                lock_path=state_lock_path,
                now=float(now_fn()),
            )
            if claimed is None:
                break
            try:
                outcome = executor(claimed)
            except Exception as error:
                outcome = {"state": "failed", "error": str(error), "result": {"outcome": "exception"}}
            if not isinstance(outcome, dict):
                outcome = {"state": "failed", "error": "worker returned an invalid result", "result": {}}
            _finish_job(
                str(claimed["id"]),
                outcome,
                state_path=state_path,
                lock_path=state_lock_path,
                now=float(now_fn()),
            )
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_UN)
    return 0


def launch_worker(
    *,
    base_dir: str = BASE_DIR,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> bool:
    """Detach a worker so queued work outlives the review process."""
    script = os.path.join(base_dir, "app_actions.py")
    if not os.path.exists(script):
        return False
    try:
        popen(
            [sys.executable or "/usr/bin/python3", script, "--worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def app_action_status(
    *,
    state_path: str = STATE_PATH,
    lock_path: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    with _locked_state(state_path, lock_path=lock_path, now=current) as state:
        jobs = [dict(job) for job in state["jobs"] if isinstance(job, dict)]
    completed = [job for job in jobs if job.get("state") == "completed"]
    completed.sort(key=lambda job: float(job.get("finished_at") or 0), reverse=True)
    failed = [job for job in jobs if job.get("state") == "failed"]
    failed.sort(key=lambda job: float(job.get("finished_at") or 0), reverse=True)
    return {
        "queued": sum(1 for job in jobs if job.get("state") == "pending"),
        "running": sum(1 for job in jobs if job.get("state") == "running"),
        "failed": len(failed),
        "most_recent_completion": completed[0] if completed else None,
        "most_recent_failure": failed[0] if failed else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help="Process pending app actions and exit.")
    parser.add_argument("--status-json", action="store_true", help="Print app-action status as JSON.")
    args = parser.parse_args(argv)
    if args.worker:
        return run_worker()
    if args.status_json:
        print(json.dumps(app_action_status(), indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
