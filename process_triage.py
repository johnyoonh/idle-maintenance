"""Deterministic review thresholds for already-classified macOS processes."""
from __future__ import annotations

from typing import Any


def _peak_cpu(proc: dict[str, Any]) -> float:
    values = proc.get("cpu_samples")
    if not isinstance(values, list) or not values:
        values = [proc.get("cpu", 0)]
    parsed = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(parsed, default=0.0)


def _peak_io(proc: dict[str, Any], key: str, fallback_key: str) -> float:
    values = proc.get("io_samples")
    parsed = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                parsed.append(float(value.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
    try:
        fallback = float(proc.get(fallback_key, 0) or 0)
    except (TypeError, ValueError):
        fallback = 0.0
    return max(parsed + [fallback], default=0.0)


def triage_process(
    proc: dict[str, Any],
    guidance: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
    *,
    peak_total_mib_s: float | None = None,
    peak_write_mib_s: float | None = None,
    recurrence: bool = False,
) -> dict[str, Any]:
    """Return suppress/review without authorizing any corrective action."""
    cfg = config or {}
    if guidance is None:
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
        return {
            "decision": "review",
            "classification": "routine-known",
            "reason": "Known routine work recurred within the review window.",
            "guidance": guidance,
        }

    try:
        multiplier = max(1.0, float(cfg.get("process_routine_review_multiplier", 4.0)))
    except (TypeError, ValueError):
        multiplier = 4.0
    cpu_multiplier = max(1.0, float(guidance.get("cpu_review_multiplier", 0) or multiplier))
    io_multiplier = max(1.0, float(guidance.get("io_review_multiplier", 0) or multiplier))
    cpu_limit = max(0.0, float(cfg.get("process_high_cpu_threshold", 50.0))) * cpu_multiplier
    total_limit = max(0.0, float(cfg.get("process_high_io_total_mib_per_second", 20.0))) * io_multiplier
    write_limit = max(0.0, float(cfg.get("process_high_io_write_mib_per_second", 10.0))) * io_multiplier

    peak_cpu = _peak_cpu(proc)
    measured_total = _peak_io(proc, "total_mib_s", "average_total_mib_s")
    measured_write = _peak_io(proc, "write_mib_s", "average_write_mib_s")
    if peak_total_mib_s is not None:
        measured_total = max(measured_total, float(peak_total_mib_s))
    if peak_write_mib_s is not None:
        measured_write = max(measured_write, float(peak_write_mib_s))

    exceeded = []
    if cpu_limit and peak_cpu >= cpu_limit:
        exceeded.append(f"CPU {peak_cpu:.1f}% >= {cpu_limit:.1f}%")
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
