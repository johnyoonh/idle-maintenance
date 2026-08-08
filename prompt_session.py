"""Persistent JSON-line client for the AppKit maintenance review window."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
from typing import Any


class PromptSession:
    """Reuse one Swift/AppKit process for a sequence of maintenance reviews."""

    def __init__(self, base_dir: str, runner=subprocess.Popen):
        self.base_dir = base_dir
        self.runner = runner
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    @property
    def script_path(self) -> str:
        return os.path.join(self.base_dir, "prompt.swift")

    def _start(self) -> subprocess.Popen[str]:
        process = self.process
        if process is not None and process.poll() is None:
            return process
        process = self.runner(
            ["swift", self.script_path, "--session"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.process = process
        return process

    def ask(self, payload: dict[str, Any]) -> str:
        """Send one review to the persistent window and wait for its disposition."""
        with self.lock:
            process = self._start()
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("review session did not expose stdin/stdout")
            try:
                process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                process.stdin.flush()
                response = process.stdout.readline()
            except (BrokenPipeError, OSError) as error:
                self.close()
                raise RuntimeError(f"review session communication failed: {error}") from error
            if not response:
                code = process.poll()
                self.close()
                raise RuntimeError(f"review session closed without a response (exit {code})")
            return response.strip().upper()

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            if process is None:
                return
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except OSError:
                    pass
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass


_SESSIONS: dict[str, PromptSession] = {}


def get_session(base_dir: str) -> PromptSession:
    key = os.path.abspath(base_dir)
    session = _SESSIONS.get(key)
    if session is None:
        session = PromptSession(key)
        _SESSIONS[key] = session
    return session


def legacy_prompt(base_dir: str, payload: dict[str, Any]) -> str:
    """Fallback to the historical one-window-per-question invocation."""
    command = [
        "swift",
        os.path.join(base_dir, "prompt.swift"),
        str(payload.get("name", "")),
        str(payload.get("path", "")),
        str(bool(payload.get("closeOnUnfocus", False))).lower(),
        str(payload.get("mode", "app")),
        str(payload.get("detail", "")),
        str(payload.get("snoozeHours", 24)),
        str(payload.get("keepDays", 1)),
        str(payload.get("policy", "review-only")),
        str(payload.get("copyText", "")),
        str(int(payload.get("pending", 0) or 0)),
        str(payload.get("headline", "")),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"swift exited {completed.returncode}").strip()
        raise RuntimeError(detail.splitlines()[-1][:500] if detail else "review prompt failed")
    return completed.stdout.strip().upper()


def ask_review(base_dir: str, payload: dict[str, Any]) -> str:
    """Prefer the persistent UI, with a compatibility fallback if it cannot start."""
    try:
        return get_session(base_dir).ask(payload)
    except RuntimeError:
        return legacy_prompt(base_dir, payload)


def close_review_session(base_dir: str | None = None) -> None:
    if base_dir is not None:
        key = os.path.abspath(base_dir)
        session = _SESSIONS.pop(key, None)
        if session is not None:
            session.close()
        return
    sessions = list(_SESSIONS.values())
    _SESSIONS.clear()
    for session in sessions:
        session.close()


atexit.register(close_review_session)
