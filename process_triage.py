"""Deterministic triage for recurring, well-understood macOS background processes."""
from __future__ import annotations

import os
from typing import Any

ROUTINE_PROCESS_PROFILES = (
    {
        "family": "spotlight-indexing",
        "label": "Spotlight/search indexing",
        "names": frozenset({"mds", "mds_stores", "corespotlightd"}),
        "prefixes": ("mdworker", "corespotlight"),
        "normal_causes": "Search-index catch-up after file churn, software updates, or storage changes.",
        "default_action": "Leave it running; review only if it repeatedly returns, causes user-visible slowdown, or becomes unusually extreme.",
    },
    {
        "family": "photos-analysis",
        "label": "Photos/media analysis",
        "names": frozenset({"mediaanalysisd", "photoanalysisd", "photolibraryd"}),
        "prefixes": ("mediaanalysis", "photoanalysis", "photolibrary"),
        "normal_causes": "Photo-library indexing, face/object analysis, import processing, or iCloud Photos catch-up.",
        "default_action": "Leave it running so background analysis can finish; review repeated or extreme activity instead of terminating it.",
    },
    {
        "family": "cloud-sync",
        "label": "iCloud/File Provider synchronization",
        "names": frozenset({"fileproviderd", "bird", "cloudd"}),
        "prefixes": ("fileprovider",),
        "normal_causes": "Cloud file reconciliation, hydration/eviction, metadata updates, or sync catch-up.",
        "default_action": "Leave synchronization running; investigate only repeated/extreme activity or a visible sync problem.",
    },
    {
        "family": "contacts-and-suggestions",
        "label": "Contacts/suggestions indexing",
        "names": frozenset({"contactsd", "suggestd", "knowledge-agent"}),
        "prefixes": ("contacts",),
        "normal_causes": "Contacts synchronization or local suggestion/knowledge indexing.",
        "default_action": "Leave it running unless repeated incidents line up with a user-visible sync or responsiveness problem.",
    },
    {
        "family": "system-maintenance",
        "label": "macOS backup/update maintenance",
        "names": frozenset({"backupd", "backupd-helper", "softwareupdated", "installd"}),
        "prefixes": ("backupd",),
        "normal_causes": "Time Machine work or an Apple software install/update performing expected bulk I/O.",
        "default_action": "Let the maintenance operation finish; review only if it repeatedly restarts, stalls, or exceeds the escalation ceiling.",
    },
)


def base_name(proc: dict[str, Any]) -> str:
    command = str(proc.get("command") or "").strip()
    token = (command.split() or [str(proc.get("comm") or "process")])[0]
    return (os.path.basename(token) or os.path.basename(str(proc.get("comm") or "")) or "process").lower()


def routine_process_profile(proc: dict[str, Any]) -> dict[str, str] | None:
    """Return a stable, reviewable knowledge profile for known routine macOS work."""
    name = base_name(proc)
    for profile in ROUTINE_PROCESS_PROFILES:
        if name in profile["names"] or name.startswith(profile["prefixes"]):
            return {
                "family": str(profile["family"]),
                "label": str(profile["label"]),
                "normal_causes": str(profile["normal_causes"]),
                "default_action": str(profile["default_action"]),
            }
    return None


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
    config: dict[str, Any] | None = None,
    *,
    peak_total_mib_s: float | None = None,
    peak_write_mib_s: float | None = None,
    recurrence: bool = False,
) -> dict[str, Any]:
    """Decide whether a known routine process needs user review.

    Suppression only removes the alarm/prompt. The incident may still be recorded by
    the caller, and this helper never authorizes termination.
    """
    cfg = config or {}
    profile = routine_process_profile(proc)
    if profile is None:
        return {
            "decision": "review",
            "classification": "unknown",
            "reason": "No routine macOS process profile matched.",
            "profile": None,
        }

    if not bool(cfg.get("process_routine_suppression_enabled", True)):
        return {
            "decision": "review",
            "classification": "routine-known",
            "reason": "Routine-process suppression is disabled by configuration.",
            "profile": profile,
        }

    if recurrence:
        return {
            "decision": "review",
            "classification": "routine-known",
            "reason": "Known routine work recurred within the review window.",
            "profile": profile,
        }

    try:
        multiplier = max(1.0, float(cfg.get("process_routine_review_multiplier", 4.0)))
    except (TypeError, ValueError):
        multiplier = 4.0
    cpu_limit = max(0.0, float(cfg.get("process_high_cpu_threshold", 50.0))) * multiplier
    total_limit = max(0.0, float(cfg.get("process_high_io_total_mib_per_second", 20.0))) * multiplier
    write_limit = max(0.0, float(cfg.get("process_high_io_write_mib_per_second", 10.0))) * multiplier

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
            "profile": profile,
        }

    return {
        "decision": "suppress",
        "classification": "routine-known",
        "reason": "Known routine macOS work is below the recurrence/extreme-use review ceiling.",
        "profile": profile,
    }


def candidates_requiring_review(
    candidates: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Filter and annotate sampled candidates using the live-monitor triage policy."""
    result = []
    for proc in candidates:
        triage = triage_process(proc, config)
        if triage["decision"] == "suppress":
            continue
        annotated = dict(proc)
        annotated["resource_triage"] = triage
        result.append(annotated)
    return result
