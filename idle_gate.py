"""Shared HID-idle helpers for automatic review presentation."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Callable


def read_idle_seconds(command_runner: Callable[..., Any] | None = None) -> float | None:
    """Read HID idle time without privileges; return unknown when unavailable."""
    runner = command_runner or subprocess.run
    try:
        result = runner(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
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
) -> float:
    """Wait for a known HID sample to meet the configured idle window."""
    delay = max(1.0, float(poll_interval))
    while True:
        idle_seconds = idle_provider()
        if idle_window_contains(idle_seconds, minimum_seconds, maximum_seconds):
            return float(idle_seconds)
        sleep_fn(delay)
