"""Deterministic review thresholds for already-classified macOS processes."""
from __future__ import annotations

import math
from typing import Any


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bounded_number(value: Any, default: float, *, minimum: float = 0.0) -> float:
    parsed = _finite_float(value)
    if parsed is None:
        parsed = default
    return max(minimum, parsed)


def _peak_cpu(proc: dict[str, Any]) -> float:
    values = proc.get("cpu_samples")
    if not isinstance(values, list) or not values:
        values = [proc.get("cpu", 0)]
    parsed = []
    for value in values:
        number = _finite_float(value)
        if number is not None:
            parsed.append(number)
    return max(parsed, default=0.0)


def _peak_io(proc: dict[str, Any], key: str, fallback_key: str) -> float:
    values = proc.get("io_samples")
    parsed = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            number = _finite_float(value.get(key, 0) or 0)
            if number is not None:
                parsed.append(number)
    fallback = _finite_float(proc.get(fallback_key, 0) or 0)
    return max(parsed + ([fallback] if fallback is not None else []), default=0.0)


def triage_process(
    proc: dict[str, Any],
    guidance: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
    *,
    peak_total_mib_s: float | None = None,
    peak_write_mib_s: float | None = None,
    recurrence: bool = False,
    recurrence_count: int | None = None,
    window_count: int | None = None,
) -> dict[str, Any]:
    """Return suppress/review/log-only without authorizing any corrective action."""
    cfg = config or {}
    measured_windows = (
        int(window_count)
        if window_count is not None
        else int(proc.get("window_count", 0) or 0)
    )
    if measured_windows <= 0:
        samples = proc.get("io_samples") or proc.get("cpu_samples") or []
        measured_windows = len(samples) if isinstance(samples, list) and samples else 2

    if guidance is None:
        min_windows = max(1, int(cfg.get("resource_monitor_min_consecutive_windows", 2)))
        total_base = _bounded_number(cfg.get("process_high_io_total_mib_per_second", 30.0), 30.0)
        write_base = _bounded_number(cfg.get("process_high_io_write_mib_per_second", 15.0), 15.0)
        cpu_base = _bounded_number(cfg.get("process_high_cpu_threshold", 75.0), 75.0)
        measured_total = max(_peak_io(proc, "total_mib_s", "average_total_mib_s"), _finite_float(peak_total_mib_s) or 0.0)
        measured_write = max(_peak_io(proc, "write_mib_s", "average_write_mib_s"), _finite_float(peak_write_mib_s) or 0.0)
        peak_cpu = _peak_cpu(proc)
        extreme_spike = (
            (total_base and measured_total >= total_base * 2.0)
            or (write_base and measured_write >= write_base * 2.0)
            or (cpu_base and peak_cpu >= cpu_base * 2.0)
        )
        if not recurrence and measured_windows < min_windows and not extreme_spike:
            return {
                "decision": "log-only",
                "classification": "unconfirmed",
                "reason": "Single sampling window spike; waiting for sustained confirmation before prompting.",
                "guidance": None,
            }
        return {
            "decision": "review",
            "classification": "unknown",
            "reason": "No known macOS process guidance matched.",
            "guidance": None,
        }
    if not bool(cfg.get("process_routine_suppression_enabled", True)):
        return {
            "decision": "review",
            "classification": "routine-known",
            "reason": "Routine-process suppression is disabled by configuration.",
            "guidance": guidance,
        }
    if recurrence:
        threshold_raw = cfg.get("process_routine_recurrence_threshold")
        threshold = int(threshold_raw) if threshold_raw is not None else None
        count = int(recurrence_count) if recurrence_count is not None else 1
        if threshold is None or threshold <= 0 or count >= threshold:
            return {
                "decision": "review",
                "classification": "routine-known",
                "reason": "Known routine work recurred within the review window.",
                "guidance": guidance,
            }

    multiplier = _bounded_number(
        cfg.get("process_routine_review_multiplier", 4.0),
        4.0,
        minimum=1.0,
    )
    cpu_multiplier = _bounded_number(
        guidance.get("cpu_review_multiplier", 0) or multiplier,
        multiplier,
        minimum=1.0,
    )
    io_multiplier = _bounded_number(
        guidance.get("io_review_multiplier", 0) or multiplier,
        multiplier,
        minimum=1.0,
    )
    cpu_limit = _bounded_number(
        cfg.get("process_high_cpu_threshold", 75.0),
        75.0,
    ) * cpu_multiplier
    mem_limit = _bounded_number(
        cfg.get("process_high_memory_mib", 3072.0),
        3072.0,
    ) * multiplier
    total_limit = _bounded_number(
        cfg.get("process_high_io_total_mib_per_second", 30.0),
        30.0,
    ) * io_multiplier
    write_limit = _bounded_number(
        cfg.get("process_high_io_write_mib_per_second", 15.0),
        15.0,
    ) * io_multiplier

    peak_cpu = _peak_cpu(proc)
    peak_mem = _finite_float(proc.get("memory_mib")) or 0.0
    measured_total = _peak_io(proc, "total_mib_s", "average_total_mib_s")
    measured_write = _peak_io(proc, "write_mib_s", "average_write_mib_s")
    provided_total = _finite_float(peak_total_mib_s)
    if provided_total is not None:
        measured_total = max(measured_total, provided_total)
    provided_write = _finite_float(peak_write_mib_s)
    if provided_write is not None:
        measured_write = max(measured_write, provided_write)

    exceeded = []
    if cpu_limit and peak_cpu >= cpu_limit:
        exceeded.append(f"CPU {peak_cpu:.1f}% >= {cpu_limit:.1f}%")
    if mem_limit and peak_mem >= mem_limit:
        exceeded.append(f"Memory {peak_mem:.0f} MiB >= {mem_limit:.0f} MiB")
    if total_limit and measured_total >= total_limit:
        exceeded.append(f"I/O {measured_total:.1f} MiB/s >= {total_limit:.1f} MiB/s")
    if write_limit and measured_write >= write_limit:
        exceeded.append(f"writes {measured_write:.1f} MiB/s >= {write_limit:.1f} MiB/s")
    if exceeded:
        return {
            "decision": "review",
            "classification": "routine-known",
            "reason": "Known routine work exceeded the review ceiling: " + ", ".join(exceeded) + ".",
            "guidance": guidance,
        }
    return {
        "decision": "suppress",
        "classification": "routine-known",
        "reason": "Known routine process work is below the recurrence/extreme-use review ceiling.",
        "guidance": guidance,
    }
