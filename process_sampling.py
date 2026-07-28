"""Sustained CPU and disk-I/O process sampling."""
from __future__ import annotations

import time

from process_identity import same, snapshot

MIB = 1024 * 1024
ATTRIBUTION_NOTE = (
    "I/O charged to the process during the sampled window; "
    "not definitive physical-disk attribution"
)


def _at(samples, target):
    return next((value for when, value in samples if abs(when - target) < 0.001), {})


def get_candidate_processes(config, snapshot_provider=None, sleep_fn=time.sleep):
    provider = snapshot_provider or (lambda: snapshot(config))
    cpu_count = max(1, int(config.get("process_cpu_sample_count", 3)))
    cpu_step = max(0.0, float(config.get("process_cpu_sample_interval_seconds", 30)))
    io_on = bool(config.get("process_io_enabled", False))
    io_count = max(2, int(config.get("process_io_sample_count", 3))) if io_on else 0
    io_step = max(0.0, float(config.get("process_io_sample_interval_seconds", 10)))
    cpu_times = [i * cpu_step for i in range(cpu_count)]
    io_times = [i * io_step for i in range(io_count)] if io_on else []
    times = sorted(set(cpu_times + io_times)) or [0.0]
    samples = []
    previous = 0.0
    for when in times:
        if when > previous:
            sleep_fn(when - previous)
        samples.append((when, provider()))
        previous = when
    first = samples[0][1]
    cpu_limit = float(config.get("process_high_cpu_threshold", 50))
    long_seconds = int(config.get("process_long_running_hours", 24)) * 3600
    long_cpu = float(config.get("process_long_running_min_cpu", 10))
    total_limit = float(config.get("process_high_io_total_mib_per_second", 20))
    write_limit = float(config.get("process_high_io_write_mib_per_second", 10))
    min_bytes = float(config.get("process_io_minimum_window_mib", 256)) * MIB
    required = max(1, int(config.get("process_io_required_intervals", 2)))
    found = []
    for pid, initial in first.items():
        reasons = []
        proc = dict(initial)
        cpu_values = []
        for when in cpu_times:
            current = _at(samples, when).get(pid)
            if not same(initial, current):
                cpu_values = []
                break
            cpu_values.append(float(current.get("cpu", 0)))
            proc.update(current)
        if len(cpu_values) == cpu_count and all(value >= cpu_limit for value in cpu_values):
            reasons.append(
                f"CPU stayed at or above {cpu_limit:.1f}% for {cpu_count} samples over {cpu_times[-1]:.0f}s"
            )
        if initial.get("elapsed_seconds", 0) >= long_seconds and initial.get("cpu", 0) >= long_cpu:
            reasons.append(
                f"Running {initial.get('etime')} (limit {long_seconds // 3600}h) with CPU at or above {long_cpu:.1f}%"
            )
        io_rates = []
        total_bytes = 0
        hot = 0
        if io_on:
            previous_proc = None
            previous_time = None
            for when in io_times:
                current = _at(samples, when).get(pid)
                if not same(initial, current) or "io_read_bytes" not in current:
                    previous_proc = None
                    break
                if previous_proc is not None:
                    seconds = when - previous_time
                    read = current["io_read_bytes"] - previous_proc["io_read_bytes"]
                    written = current["io_write_bytes"] - previous_proc["io_write_bytes"]
                    if seconds <= 0 or read < 0 or written < 0:
                        io_rates = []
                        break
                    total_bytes += read + written
                    total_rate = (read + written) / MIB / seconds
                    write_rate = written / MIB / seconds
                    io_rates.append(
                        {
                            "read_mib_s": read / MIB / seconds,
                            "write_mib_s": write_rate,
                            "total_mib_s": total_rate,
                        }
                    )
                    if total_rate >= total_limit or write_rate >= write_limit:
                        hot += 1
                previous_proc, previous_time = current, when
                proc.update(current)
            if hot >= required and total_bytes >= min_bytes:
                peak = max(item["total_mib_s"] for item in io_rates)
                peak_write = max(item["write_mib_s"] for item in io_rates)
                average = sum(item["total_mib_s"] for item in io_rates) / len(io_rates)
                proc["average_total_mib_s"] = average
                proc["average_write_mib_s"] = sum(item["write_mib_s"] for item in io_rates) / len(io_rates)
                reasons.append(
                    f"I/O charged to the process averaged {average:.1f} MiB/s across {hot} sustained intervals; "
                    f"peak {peak:.1f} MiB/s total, {peak_write:.1f} MiB/s writes; {ATTRIBUTION_NOTE}"
                )
        if reasons:
            proc.setdefault("process_key", __import__("process_identity").key(proc))
            proc["cpu_samples"] = cpu_values or [proc.get("cpu", 0)]
            proc["io_samples"] = io_rates
            proc["reason"] = " • ".join(reasons)
            proc["impact_score"] = max(
                [proc.get("cpu", 0)] + [item["total_mib_s"] * 4 for item in io_rates]
            )
            found.append(proc)
    return sorted(found, key=lambda proc: (-proc["impact_score"], -proc.get("elapsed_seconds", 0)))
