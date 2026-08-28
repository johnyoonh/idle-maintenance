"""Install safe, resource-aware process review into the existing maintenance core."""
from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import subprocess
import time
from typing import Any

from idle_config import APP_SUPPORT_DIR, atomic_write_json, keep_entry_is_active, load_config, next_keep_delay_days
import process_identity as identity
from process_sampling import get_candidate_processes
from process_triage import triage_process
from prompt_session import legacy_prompt

_LOCK = None
ATTRIBUTION_NOTE = (
    "I/O charged to the process during the sampled window; "
    "this is not definitive physical-disk attribution."
)

KNOWN_PROCESS_PROFILES = (
    {
        "names": set(),
        "prefixes": (),
        "command_contains": ("Google Drive.app/Contents/MacOS/Google Drive",),
        "recurrence_group": "google-drive-sync",
        "role": "Google Drive synchronization and offline-file reconciliation",
        "default_action": "Allow expected downloads, uploads, offline pinning, hashing, and metadata reconciliation to settle. If CPU remains near one full core after Drive reports it is current, use a user-initiated graceful quit and reopen.",
        "action_policy": "graceful-quit",
        "cpu_review_multiplier": 2.0,
        "io_review_multiplier": 4.0,
    },
    {
        "names": {"mds", "mds_stores", "corespotlightd"},
        "prefixes": ("mdworker", "corespotlight"),
        "recurrence_group": "spotlight-indexing",
        "role": "Spotlight/Core Spotlight indexing",
        "default_action": "Leave it running. If high usage keeps recurring after indexing should settle, inspect recent indexing churn or excluded paths instead of terminating the daemon.",
    },
    {
        "names": {"mediaanalysisd", "photoanalysisd"},
        "prefixes": ("mediaanalysis", "photoanalysis"),
        "recurrence_group": "photos-analysis",
        "role": "Photos and media analysis",
        "default_action": "Leave it running while imports, face/object analysis, or photo-library synchronization settle; investigate only if the load remains sustained or repeatedly returns.",
    },
    {
        "names": {"fileproviderd", "bird", "cloudd"},
        "prefixes": ("fileprovider",),
        "recurrence_group": "cloud-sync",
        "role": "iCloud/File Provider synchronization",
        "default_action": "Leave it running and check the relevant sync provider or backlog if activity persists. Do not kill the sync daemon as a first response.",
    },
    {
        "names": {"contactsd"},
        "prefixes": ("contacts",),
        "recurrence_group": "contacts-sync",
        "role": "Contacts synchronization and indexing",
        "default_action": "Leave it running. If the activity repeatedly returns, inspect account synchronization state before considering application-level changes.",
    },
    {
        "names": {"suggestd", "knowledge-agent"},
        "prefixes": (),
        "recurrence_group": "suggestions-indexing",
        "role": "Apple suggestions/knowledge indexing",
        "default_action": "Leave it running unless sustained measurements keep recurring; prefer identifying the source data or recent system activity over terminating the service.",
    },
    {
        "names": {"backupd", "backupd-helper"},
        "prefixes": (),
        "recurrence_group": "time-machine",
        "role": "Time Machine backup",
        "default_action": "Allow an expected backup to finish. If activity is unexpected or repeatedly stalls, inspect Time Machine status and the backup destination rather than killing backupd.",
    },
    {
        "names": {"softwareupdated", "storedownloadd", "installd"},
        "prefixes": (),
        "recurrence_group": "software-update",
        "role": "macOS software download/update installation",
        "default_action": "Allow an expected update to finish. If activity is recurrent without visible progress, inspect Software Update state rather than terminating the system service.",
    },
    {
        "names": {"windowserver", "powerd", "trustd", "runningboardd"},
        "prefixes": (),
        "role": "Core macOS system service",
        "default_action": "Do not terminate it. Treat sustained resource use as a symptom and inspect the applications, display workload, power state, or trust activity driving it.",
    },
)

PROTECTED_APPLE_DAEMONS = {
    name for profile in KNOWN_PROCESS_PROFILES for name in profile["names"]
}
PROTECTED_PREFIXES = tuple(
    prefix for profile in KNOWN_PROCESS_PROFILES for prefix in profile["prefixes"]
)


def _base_name(proc: dict[str, Any]) -> str:
    command = str(proc.get("command") or "").strip()
    token = (command.split() or [str(proc.get("comm") or "process")])[0]
    return (os.path.basename(token) or os.path.basename(str(proc.get("comm") or "")) or "process").lower()


def _display(proc: dict[str, Any]) -> str:
    command = str(proc.get("command") or "").strip()
    name = _base_name(proc)
    return f"{name} ({command})" if command and command.lower() != name else name


def known_process_guidance(proc: dict[str, Any]) -> dict[str, Any] | None:
    """Return deterministic handling guidance for common macOS background processes."""
    name = _base_name(proc)
    command = str(proc.get("command") or "")
    for profile in KNOWN_PROCESS_PROFILES:
        command_matches = any(
            marker.lower() in command.lower()
            for marker in profile.get("command_contains", ())
        )
        if name in profile["names"] or name.startswith(profile["prefixes"]) or command_matches:
            return {
                "name": name,
                "recurrence_group": str(profile.get("recurrence_group") or f"process:{name}"),
                "role": str(profile["role"]),
                "default_action": str(profile["default_action"]),
                "policy": "observe-first",
                "action_policy": str(profile.get("action_policy") or "protected"),
                "cpu_review_multiplier": float(profile.get("cpu_review_multiplier", 0) or 0),
                "io_review_multiplier": float(profile.get("io_review_multiplier", 0) or 0),
            }
    return None


def should_suppress_process_alert(proc: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    """Suppress understood macOS work unless recurrence/extreme use warrants review."""
    return triage_process(proc, known_process_guidance(proc), config)["decision"] == "suppress"


def process_action_policy(proc: dict[str, Any]) -> str:
    """Return protected, graceful-quit, or review-only for a process instance."""
    name = _base_name(proc)
    guidance = known_process_guidance(proc)
    if guidance and guidance.get("action_policy") == "graceful-quit":
        return "graceful-quit"
    if guidance or name in PROTECTED_APPLE_DAEMONS or name.startswith(PROTECTED_PREFIXES):
        return "protected"
    command = str(proc.get("command") or "")
    if name == "mail" and ".app/Contents/MacOS/Mail" in command:
        return "graceful-quit"
    if name == "shortcuts" and ".app/Contents/MacOS/Shortcuts" in command:
        return "graceful-quit"
    return "review-only"


def _known_context(proc: dict[str, Any]) -> list[str]:
    guidance = known_process_guidance(proc)
    if not guidance:
        return []
    return [
        f"Known macOS role: {guidance['role']}",
        f"Default handling: {guidance['default_action']}",
    ]


def prompt_process(core: Any, proc: dict[str, Any], snooze_hours: float = 24, keep_days: float = 1) -> str:
    cpu = ", ".join(f"{x:.1f}%" for x in proc.get("cpu_samples", [proc.get("cpu", 0)]))
    detail = (
        f"PID {proc['pid']} • CPU samples: {cpu} • Elapsed {proc.get('etime', '?')}\n"
        f"Reason: {proc.get('reason', 'Matched resource policy')}"
    )
    rates = proc.get("io_samples", [])
    if rates:
        detail += "\nI/O: " + ", ".join(
            f"{x['total_mib_s']:.1f} MiB/s ({x['write_mib_s']:.1f} write)" for x in rates
        )
        detail += f"\n{ATTRIBUTION_NOTE}"
    context = _known_context(proc)
    if context:
        detail += "\n" + "\n".join(context)
    resource_triage = proc.get("resource_triage")
    if isinstance(resource_triage, dict):
        detail += f"\nTriage: {resource_triage.get('decision', 'review')} — {resource_triage.get('reason', '')}"
    policy = process_action_policy(proc)
    payload = {
        "name": _display(proc),
        "path": proc.get("command") or proc.get("comm", ""),
        "closeOnUnfocus": False,
        "mode": "process",
        "detail": detail,
        "snoozeHours": snooze_hours,
        "keepDays": keep_days,
        "policy": policy,
        "copyText": investigation_prompt(core, proc),
        "pending": 1,
        "headline": process_headline(proc),
    }
    try:
        value = legacy_prompt(core.BASE_DIR, payload)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"process prompt failed: {error}") from error
    allowed = {"INVESTIGATE", "KEEP", "SNOOZE", "QUIT"}
    if policy == "graceful-quit":
        allowed.add("GRACEFUL_QUIT")
    return value if value in allowed else "QUIT"


def process_headline(proc: dict[str, Any]) -> str:
    """Return compact peak metrics for severity coloring in one-shot reviews."""
    samples = proc.get("cpu_samples")
    if not isinstance(samples, list) or not samples:
        samples = [proc.get("cpu", 0)]
    cpu_values = []
    for value in samples:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            cpu_values.append(parsed)
    rates = proc.get("io_samples")
    io_values = []
    if isinstance(rates, list):
        for rate in rates:
            if not isinstance(rate, dict):
                continue
            try:
                parsed = float(rate.get("total_mib_s", 0) or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                io_values.append(parsed)
    if io_values and max(io_values) > 0:
        return f"I/O peak {max(io_values):.1f} MiB/s • CPU peak {max(cpu_values, default=0.0):.1f}%"
    return "CPU samples: " + ", ".join(f"{value:.1f}%" for value in (cpu_values or [0.0]))


def investigation_prompt(core: Any, proc: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    del core
    guidance = known_process_guidance(proc)
    stored_triage = proc.get("resource_triage")
    resource_triage = stored_triage if isinstance(stored_triage, dict) else triage_process(proc, guidance, config)
    lines = [
        "Investigate this high-impact macOS process and help me decide what to do.",
        "",
        "Use rootless, bounded inspection only. Do not run privileged tracing, recursive cloud scans, throttling, or termination automatically.",
        "When known macOS context is supplied below, treat it as the default classification instead of re-investigating generic process identity from scratch.",
        "",
        "Please cover:",
        "1. Whether the measured behavior fits the known role or is genuinely unusual.",
        "2. Why it may be using CPU or disk I/O now.",
        "3. Which rootless observations would distinguish the likely causes.",
        "4. Whether a user-initiated graceful quit is appropriate.",
        "5. Recommended next action.",
        "",
    ]
    context = _known_context(proc)
    if context:
        lines.extend(["Known macOS context:", *[f"- {item}" for item in context]])
        lines.append(f"- Deterministic triage: {resource_triage['decision']} ({resource_triage['reason']})")
        lines.append("")
    lines.extend(
        [
            "Process details:",
            f"- PID: {proc['pid']}",
            f"- Command: {proc.get('command', '')}",
            f"- CPU: {proc.get('cpu', 0):.1f}%",
            f"- Elapsed: {proc.get('etime', '?')}",
            f"- Reason: {proc.get('reason', '')}",
            f"- Attribution boundary: {ATTRIBUTION_NOTE}",
            "",
            "Possible rootless checks to review before running:",
            f"- ps -p {proc['pid']} -o pid,ppid,uid,%cpu,etime,lstart,command",
            f"- lsof -p {proc['pid']} -Fn",
        ]
    )
    return "\n".join(lines)


def terminate(
    proc: dict[str, Any],
    config: dict[str, Any],
    identity_provider=identity.read,
    signal_fn=os.kill,
    sleep_fn=time.sleep,
    monotonic=time.monotonic,
) -> str:
    """Identity-revalidate and send SIGTERM only; never escalate automatically."""
    current = identity_provider(proc["pid"])
    if current is None:
        return "terminated"
    if not identity.same(proc, current):
        return "stale"
    try:
        signal_fn(proc["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "failed"
    deadline = monotonic() + max(0.0, float(config.get("process_terminate_grace_seconds", 5)))
    poll = max(0.05, float(config.get("process_terminate_poll_seconds", 0.25)))
    while monotonic() < deadline:
        current = identity_provider(proc["pid"])
        if current is None:
            return "terminated"
        if not identity.same(proc, current):
            return "stale"
        sleep_fn(min(poll, max(0.0, deadline - monotonic())))
    return "still_running"


def force_kill(*_args: Any, **_kwargs: Any) -> str:
    """Compatibility shim: force termination is intentionally unsupported."""
    return "unsupported"


def handle_process_action(core: Any, proc: dict[str, Any], action: str, config: dict[str, Any]) -> str:
    policy = process_action_policy(proc)
    if action == "GRACEFUL_QUIT":
        if policy != "graceful-quit":
            return "disallowed"
        outcome = terminate(proc, config)
        if outcome == "terminated":
            core.notify_user("Idle Maintenance", f"Gracefully quit {_base_name(proc)} (PID {proc['pid']}).")
        return outcome
    if action == "INVESTIGATE":
        opened, terminal_app, prompt_copied = core.open_codex_in_terminal(
            investigation_prompt(core, proc, config), core.process_cwd(proc)
        )
        if opened:
            core.log(
                f"Opened Codex investigation for {_base_name(proc)} in {terminal_app}."
            )
            return "investigating"
        message = f"Could not open an investigation tab for {_base_name(proc)}."
        if prompt_copied:
            message += " The prompt was copied to the clipboard."
        core.notify_user("Idle Maintenance", message)
        return "failed"
    return "unchanged"


def run_process_audit(core: Any, config: dict[str, Any], prompt_budget: int | None = None) -> tuple[bool, int]:
    limit = int(config.get("process_max_prompts", core.DEFAULT_MAX_PROMPTS))
    if prompt_budget is not None:
        limit = min(limit, max(0, int(prompt_budget)))
    if limit <= 0:
        return True, 0
    candidates = []
    for proc in get_candidate_processes(config):
        resource_triage = triage_process(proc, known_process_guidance(proc), config)
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
    for item in queue:
        if done >= limit:
            break
        proc = by_key.get(item["process_key"])
        if not proc or core.queue_item_is_snoozed(item, snooze):
            continue
        keep_days = next_keep_delay_days(config, whitelist.get(item["process_key"]), "process_")
        action = prompt_process(core, proc, snooze, keep_days)
        if action == "QUIT":
            core.save_json(core.PROCESS_QUEUE_PATH, current)
            core.save_json(core.PROCESS_WHITELIST_PATH, whitelist)
            return False, done
        if action == "KEEP":
            core.record_keep(whitelist, item["process_key"])
            current = [x for x in current if x["process_key"] != item["process_key"]]
        else:
            handle_process_action(core, proc, action, config)
            for entry in current:
                if entry["process_key"] == item["process_key"]:
                    entry["last_prompted"] = int(time.time())
        done += 1
    core.save_json(core.PROCESS_QUEUE_PATH, current)
    core.save_json(core.PROCESS_WHITELIST_PATH, whitelist)
    return True, done


def install(core: Any) -> None:
    global _LOCK
    if getattr(core, "_RESOURCE_REVIEW_INSTALLED", False):
        return
    core.LOCK_FILE = os.path.join(APP_SUPPORT_DIR, "interactive.lock")
    original_main = core.main

    def is_running() -> bool:
        global _LOCK
        os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
        handle = open(core.LOCK_FILE, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return True
        _LOCK = handle
        return False

    def create_lock() -> None:
        return None

    def save(path: str, data: Any) -> bool:
        return atomic_write_json(path, data)

    def main() -> Any:
        global _LOCK
        try:
            return original_main()
        finally:
            if _LOCK:
                try:
                    fcntl.flock(_LOCK.fileno(), fcntl.LOCK_UN)
                    _LOCK.close()
                except OSError:
                    pass
                _LOCK = None

    def load(path: str, default: Any = None) -> Any:
        if default is None:
            default = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return default

    core.is_running, core.create_lock, core.save_json, core.load_json = is_running, create_lock, save, load
    core.command_fingerprint = lambda comm, command: identity.fingerprint(f"{comm}\0{command}")
    core.process_key = identity.key
    core.get_process_snapshot = lambda config: identity.snapshot(config)
    core.get_candidate_processes = get_candidate_processes
    core.process_action_policy = process_action_policy
    core.known_process_guidance = known_process_guidance
    core.should_suppress_process_alert = should_suppress_process_alert
    core.triage_process = lambda proc, config=None, **kwargs: triage_process(
        proc, known_process_guidance(proc), config, **kwargs
    )
    core.prompt_process = lambda proc, snooze_hours=24, keep_days=1: prompt_process(
        core, proc, snooze_hours, keep_days
    )
    core.build_process_investigation_prompt = lambda proc, config=None: investigation_prompt(core, proc, config)
    core.terminate_process = lambda proc, config, identity_provider=identity.read, signal_fn=os.kill, sleep_fn=time.sleep, monotonic_fn=time.monotonic: terminate(
        proc, config, identity_provider, signal_fn, sleep_fn, monotonic_fn
    )
    core.force_kill_process = force_kill
    core.run_process_audit = lambda config, prompt_budget=None: run_process_audit(core, config, prompt_budget)
    core.kill_process = lambda pid: terminate(identity.read(pid) or {"pid": pid}, load_config(core.BASE_DIR)) == "terminated"
    core.main = main
    core._RESOURCE_REVIEW_INSTALLED = True
