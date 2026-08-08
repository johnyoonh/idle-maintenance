"""Persistent review-window overlay for app and process maintenance approvals."""
from __future__ import annotations

import inspect
import time
from typing import Any

from idle_config import keep_entry_is_active, next_keep_delay_days
from prompt_session import ask_review


def _cpu_samples(proc: dict[str, Any]) -> list[float]:
    values = proc.get("cpu_samples")
    if not isinstance(values, list) or not values:
        values = [proc.get("cpu", 0)]
    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            continue
    return parsed or [0.0]


def process_headline(proc: dict[str, Any]) -> str:
    """Describe the actual review trigger instead of treating CPU as universal."""
    samples = _cpu_samples(proc)
    rates = proc.get("io_samples")
    if isinstance(rates, list) and rates:
        totals = []
        writes = []
        for rate in rates:
            if not isinstance(rate, dict):
                continue
            try:
                totals.append(float(rate.get("total_mib_s", 0) or 0))
                writes.append(float(rate.get("write_mib_s", 0) or 0))
            except (TypeError, ValueError):
                continue
        if totals and max(totals) > 0:
            return (
                f"I/O trigger: peak {max(totals):.1f} MiB/s"
                f" • CPU sample {samples[-1]:.1f}%"
            )

    triage = proc.get("resource_triage")
    triage_reason = str(triage.get("reason", "")) if isinstance(triage, dict) else ""
    reason = str(proc.get("reason", ""))
    if "recur" in triage_reason.lower():
        return f"Recurrence trigger • CPU samples: {', '.join(f'{value:.1f}%' for value in samples)}"
    if "cpu" in reason.lower() or max(samples) > 0:
        return "CPU samples: " + ", ".join(f"{value:.1f}%" for value in samples)
    return "Resource-policy trigger • CPU sample 0.0%"


def _process_detail(pr: Any, proc: dict[str, Any]) -> str:
    cpu = ", ".join(f"{x:.1f}%" for x in _cpu_samples(proc))
    detail = (
        f"PID {proc['pid']} • CPU samples: {cpu} • Elapsed {proc.get('etime', '?')}\n"
        f"Reason: {proc.get('reason', 'Matched resource policy')}"
    )
    rates = proc.get("io_samples", [])
    if rates:
        detail += "\nI/O: " + ", ".join(
            f"{x['total_mib_s']:.1f} MiB/s ({x['write_mib_s']:.1f} write)" for x in rates
        )
        detail += f"\n{pr.ATTRIBUTION_NOTE}"
    context = pr._known_context(proc)
    if context:
        detail += "\n" + "\n".join(context)
    resource_triage = proc.get("resource_triage")
    if isinstance(resource_triage, dict):
        detail += f"\nTriage: {resource_triage.get('decision', 'review')} — {resource_triage.get('reason', '')}"
    return detail


def prompt_process(
    core: Any,
    pr: Any,
    proc: dict[str, Any],
    snooze_hours: float = 24,
    keep_days: float = 1,
    *,
    config: dict[str, Any] | None = None,
    pending: int = 0,
) -> str:
    policy = pr.process_action_policy(proc)
    payload = {
        "name": pr._display(proc),
        "path": proc.get("command") or proc.get("comm", ""),
        "closeOnUnfocus": False,
        "mode": "process",
        "detail": _process_detail(pr, proc),
        "snoozeHours": snooze_hours,
        "keepDays": keep_days,
        "policy": policy,
        "copyText": pr.investigation_prompt(core, proc, config),
        "pending": max(0, int(pending)),
        "headline": process_headline(proc),
    }
    value = ask_review(core.BASE_DIR, payload)
    allowed = {"INVESTIGATE", "KEEP", "SNOOZE", "QUIT"}
    if policy == "graceful-quit":
        allowed.add("GRACEFUL_QUIT")
    return value if value in allowed else "QUIT"


def _pending_app_reviews(core: Any) -> int:
    """Read the active maintenance main-loop state without changing core semantics."""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame is not None:
            local = frame.f_locals
            if {
                "current_queue",
                "processed",
                "max_prompts",
                "app_snooze_hours",
            }.issubset(local):
                queue = local.get("current_queue")
                if not isinstance(queue, list):
                    return 0
                remaining_budget = max(0, int(local.get("max_prompts", 0)) - int(local.get("processed", 0)))
                snooze = float(local.get("app_snooze_hours", 0))
                eligible = sum(
                    1
                    for item in queue
                    if isinstance(item, dict) and not core.queue_item_is_snoozed(item, snooze)
                )
                return min(remaining_budget, eligible)
            frame = frame.f_back
    finally:
        del frame
    return 0


def prompt_app(
    core: Any,
    app_path: str,
    close_on_unfocus: bool = True,
    detail: str = "",
    snooze_hours: float = 720,
    keep_days: float = 60,
) -> str:
    payload = {
        "name": __import__("os").path.basename(app_path),
        "path": app_path,
        "closeOnUnfocus": bool(close_on_unfocus),
        "mode": "app",
        "detail": detail,
        "snoozeHours": snooze_hours,
        "keepDays": keep_days,
        "policy": "review-only",
        "copyText": "",
        "pending": _pending_app_reviews(core),
        "headline": "",
    }
    value = ask_review(core.BASE_DIR, payload)
    return value if value in {"SNOOZE", "KEEP", "DELETE", "TRY", "QUIT"} else "QUIT"


def run_process_audit(
    core: Any,
    pr: Any,
    config: dict[str, Any],
    prompt_budget: int | None = None,
) -> tuple[bool, int]:
    """Process audit with exact pending counts for the active review stage."""
    limit = int(config.get("process_max_prompts", core.DEFAULT_MAX_PROMPTS))
    if prompt_budget is not None:
        limit = min(limit, max(0, int(prompt_budget)))
    if limit <= 0:
        return True, 0

    candidates = []
    for proc in pr.get_candidate_processes(config):
        resource_triage = pr.triage_process(proc, pr.known_process_guidance(proc), config)
        if resource_triage["decision"] == "suppress":
            continue
        reviewed = dict(proc)
        reviewed["resource_triage"] = resource_triage
        candidates.append(reviewed)

    by_key = {proc["process_key"]: proc for proc in candidates}
    queue = core.load_json(core.PROCESS_QUEUE_PATH)
    queue = queue if isinstance(queue, list) else []
    whitelist = core.load_custom_whitelist(core.PROCESS_WHITELIST_PATH)

    migrated = []
    for item in queue:
        key = item.get("process_key")
        if not key and item.get("comm"):
            key = next((p["process_key"] for p in candidates if p.get("comm") == item["comm"]), None)
        if key in by_key:
            migrated.append({"process_key": key, "last_prompted": item.get("last_prompted", 0)})
    queue = migrated

    existing = {x["process_key"] for x in queue}
    for proc in candidates:
        key = proc["process_key"]
        kept = keep_entry_is_active(config, whitelist.get(key), "process_") or keep_entry_is_active(
            config, whitelist.get(proc.get("comm")), "process_"
        )
        if key not in existing and not kept:
            queue.append({"process_key": key, "last_prompted": 0})
    queue.sort(key=lambda x: x.get("last_prompted", 0))

    current = list(queue)
    done = 0
    snooze = max(0.0, float(config.get("process_snooze_hours", 24)))
    reviewable = [
        item
        for item in queue
        if by_key.get(item.get("process_key")) and not core.queue_item_is_snoozed(item, snooze)
    ]
    pending_total = min(limit, len(reviewable))

    for item in queue:
        if done >= limit:
            break
        proc = by_key.get(item["process_key"])
        if not proc or core.queue_item_is_snoozed(item, snooze):
            continue
        keep_days = next_keep_delay_days(config, whitelist.get(item["process_key"]), "process_")
        action = prompt_process(
            core,
            pr,
            proc,
            snooze,
            keep_days,
            config=config,
            pending=max(1, pending_total - done),
        )
        if action == "QUIT":
            core.save_json(core.PROCESS_QUEUE_PATH, current)
            core.save_json(core.PROCESS_WHITELIST_PATH, whitelist)
            return False, done
        if action == "KEEP":
            core.record_keep(whitelist, item["process_key"])
            current = [x for x in current if x["process_key"] != item["process_key"]]
        else:
            pr.handle_process_action(core, proc, action, config)
            for entry in current:
                if entry["process_key"] == item["process_key"]:
                    entry["last_prompted"] = int(time.time())
        done += 1

    core.save_json(core.PROCESS_QUEUE_PATH, current)
    core.save_json(core.PROCESS_WHITELIST_PATH, whitelist)
    return True, done


def install(core: Any, process_review_module: Any) -> None:
    """Overlay only review presentation/session behavior on the compatibility core."""
    pr = process_review_module
    core.prompt_user = lambda app_path, close_on_unfocus=True, detail="", snooze_hours=720, keep_days=60: prompt_app(
        core, app_path, close_on_unfocus, detail, snooze_hours, keep_days
    )
    core.prompt_process = lambda proc, snooze_hours=24, keep_days=1: prompt_process(
        core, pr, proc, snooze_hours, keep_days
    )
    core.run_process_audit = lambda config, prompt_budget=None: run_process_audit(
        core, pr, config, prompt_budget
    )
    core._PERSISTENT_REVIEW_UI_INSTALLED = True
