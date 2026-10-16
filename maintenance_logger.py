#!/usr/bin/env python3
"""Unified logging for idle maintenance timeouts, slowdowns, and diagnostic tracing."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "IdleMaintenance.log")
SLOW_LOG_PATH = os.path.join(LOG_DIR, "idle-maintenance", "slowdown.log")

DEFAULT_SLOW_THRESHOLD_SECONDS = 2.0


def log(msg: str, *, echo: bool = False, path: str | None = None) -> None:
    """Append a timestamped message to IdleMaintenance.log."""
    target = path or LOG_PATH
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(formatted)
    except OSError:
        pass
    if echo:
        print(msg, file=sys.stderr)


def log_slowdown(
    component: str,
    operation: str,
    duration: float,
    threshold: float = 0.0,
    details: str = "",
    *,
    extra: dict[str, Any] | None = None,
    log_path: str | None = None,
    slow_log_path: str | None = None,
) -> str:
    """Log an operation that completed but took longer than its slow threshold."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    thresh_part = f" (threshold {threshold:.2f}s)" if threshold > 0 else ""
    detail_part = f": {details}" if details else ""
    msg = f"[SLOW] {component}: {operation} took {duration:.2f}s{thresh_part}{detail_part}"

    # 1. Main log
    log(msg, path=log_path)

    # 2. Structured JSONL slowdown log
    slow_target = slow_log_path or SLOW_LOG_PATH
    try:
        os.makedirs(os.path.dirname(slow_target), exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": time.time(),
            "datetime": timestamp,
            "type": "slowdown",
            "component": component,
            "operation": operation,
            "duration": round(duration, 3),
            "threshold": round(threshold, 3) if threshold > 0 else None,
            "details": details,
        }
        if extra:
            entry.update(extra)
        with open(slow_target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass
    return msg


def log_timeout(
    component: str,
    operation: str,
    timeout: float,
    details: str = "",
    *,
    extra: dict[str, Any] | None = None,
    log_path: str | None = None,
    slow_log_path: str | None = None,
) -> str:
    """Log an operation that timed out before completion."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    detail_part = f": {details}" if details else ""
    msg = f"[TIMEOUT] {component}: {operation} timed out after {timeout:.2f}s{detail_part}"

    # 1. Main log
    log(msg, path=log_path)

    # 2. Structured JSONL slowdown log
    slow_target = slow_log_path or SLOW_LOG_PATH
    try:
        os.makedirs(os.path.dirname(slow_target), exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": time.time(),
            "datetime": timestamp,
            "type": "timeout",
            "component": component,
            "operation": operation,
            "timeout": round(timeout, 3),
            "details": details,
        }
        if extra:
            entry.update(extra)
        with open(slow_target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass
    return msg


@contextlib.contextmanager
def track_slowdown(
    component: str,
    operation: str,
    threshold: float = DEFAULT_SLOW_THRESHOLD_SECONDS,
    *,
    details: str = "",
    extra: dict[str, Any] | None = None,
    log_path: str | None = None,
    slow_log_path: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Context manager to measure execution time, logging on timeout or slow completion."""
    start = time.perf_counter()
    context: dict[str, Any] = {"details": details}
    try:
        yield context
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - start
        t = getattr(exc, "timeout", threshold)
        t_val = float(t) if t is not None else threshold
        detail_str = str(exc).strip() or str(context.get("details", ""))
        log_timeout(
            component,
            operation,
            timeout=t_val,
            details=detail_str,
            extra=extra,
            log_path=log_path,
            slow_log_path=slow_log_path,
        )
        raise
    except Exception as exc:
        duration = time.perf_counter() - start
        if duration >= threshold:
            err_detail = f"{type(exc).__name__}: {exc}"
            if context.get("details"):
                err_detail = f"{context['details']} ({err_detail})"
            log_slowdown(
                component,
                operation,
                duration=duration,
                threshold=threshold,
                details=err_detail,
                extra=extra,
                log_path=log_path,
                slow_log_path=slow_log_path,
            )
        raise
    else:
        duration = time.perf_counter() - start
        if duration >= threshold:
            log_slowdown(
                component,
                operation,
                duration=duration,
                threshold=threshold,
                details=str(context.get("details", "")),
                extra=extra,
                log_path=log_path,
                slow_log_path=slow_log_path,
            )


def load_recent_slowdowns(
    limit: int = 5,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load recent slowdown/timeout records from the JSONL log."""
    log_path = Path(path or SLOW_LOG_PATH)
    if not log_path.exists():
        return []
    try:
        lines = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        try:
            val = json.loads(line)
            if isinstance(val, dict):
                records.append(val)
        except json.JSONDecodeError:
            continue
    return records
