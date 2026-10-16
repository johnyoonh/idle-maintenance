"""Shared HID-idle helpers for automatic review presentation."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Callable


from maintenance_logger import log_slowdown, log_timeout


def read_idle_seconds(
    command_runner: Callable[..., Any] | None = None,
    *,
    slow_threshold: float = 2.0,
) -> float | None:
    """Read HID idle time without privileges; return unknown when unavailable."""
    runner = command_runner or subprocess.run
    start = time.perf_counter()
    try:
        result = runner(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_timeout("idle_gate", "read_idle_seconds (ioreg)", timeout=5.0, details=str(exc))
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    duration = time.perf_counter() - start
    if duration >= slow_threshold:
        log_slowdown("idle_gate", "read_idle_seconds (ioreg)", duration=duration, threshold=slow_threshold)
    if result.returncode != 0:
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout or "")
    return int(match.group(1)) / 1_000_000_000 if match else None


def idle_window_contains(
    idle_seconds: float | None,
    minimum_seconds: float,
    maximum_seconds: float = 0,
) -> bool:
    """Return whether a known idle sample is in the configured review window.

    A nonpositive maximum means there is no upper bound.
    """
    if idle_seconds is None:
        return False
    idle = float(idle_seconds)
    minimum = max(0.0, float(minimum_seconds))
    maximum = float(maximum_seconds)
    return idle >= minimum and (maximum <= 0 or idle < max(minimum, maximum))


def wait_until_idle(
    minimum_seconds: float,
    *,
    maximum_seconds: float = 0,
    idle_provider: Callable[[], float | None] = read_idle_seconds,
    poll_interval: float = 30,
    sleep_fn: Callable[[float], None] = time.sleep,
    slow_threshold: float = 10.0,
) -> float:
    """Wait for a known HID sample to meet the configured idle window."""
    delay = max(1.0, float(poll_interval))
    start = time.perf_counter()
    last_warning = start
    while True:
        idle_seconds = idle_provider()
        now = time.perf_counter()
        elapsed = now - start
        if idle_window_contains(idle_seconds, minimum_seconds, maximum_seconds):
            if elapsed >= slow_threshold:
                log_slowdown(
                    "idle_gate",
                    "wait_until_idle",
                    duration=elapsed,
                    threshold=slow_threshold,
                    details=f"waited {elapsed:.1f}s for {minimum_seconds:.0f}s idle window",
                )
            return float(idle_seconds)
        if maximum_seconds > 0 and elapsed >= maximum_seconds:
            log_timeout(
                "idle_gate",
                "wait_until_idle",
                timeout=maximum_seconds,
                details=f"target {minimum_seconds:.0f}s not reached within {maximum_seconds:.0f}s limit",
            )
            return float(idle_seconds) if idle_seconds is not None else 0.0
        if elapsed >= slow_threshold and (now - last_warning) >= 60.0:
            last_warning = now
            log_slowdown(
                "idle_gate",
                "wait_until_idle",
                duration=elapsed,
                threshold=slow_threshold,
                details=f"still waiting ({elapsed:.1f}s elapsed, target {minimum_seconds:.0f}s, current idle {idle_seconds}s)",
            )
        sleep_fn(delay)

