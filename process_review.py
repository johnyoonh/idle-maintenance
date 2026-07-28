"""Install safe, resource-aware process review into the existing maintenance core."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from typing import Any

from idle_config import APP_SUPPORT_DIR, atomic_write_json, keep_entry_is_active, load_config, next_keep_delay_days
import process_identity as identity
from process_sampling import get_candidate_processes

_LOCK = None
ATTRIBUTION_NOTE = (
    "I/O charged to the process during the sampled window; "
    "this is not definitive physical-disk attribution."
)
PROTECTED_APPLE_DAEMONS = {
    "mediaanalysisd",
    "photoanalysisd",
    "contactsd",
    "corespotlightd",
    "mds",
    "mds_stores",
    "fileproviderd",
    "bird",
    "cloudd",
    "suggestd",
    "knowledge-agent",
}
PROTECTED_PREFIXES = (
    "mdworker",
    "mediaanalysis",
    "photoanalysis",
    "contacts",
    "corespotlight",
    "fileprovider",
)


def _base_name(proc: dict[str, Any]) -> str:
    command = str(proc.get("command") or "").strip()
    token = (command.split() or [str(proc.get("comm") or "process")])[0]
    return (os.path.basename(token) or os.path.basename(str(proc.get("comm") or "")) or "process").lower()


def _display(proc: dict[str, Any]) -> str:
    command = str(proc.get("command") or "").strip()
    name = _base_name(proc)
    return f"{name} ({command})" if command and command.lower() != name else name


def process_action_policy(proc: dict[str, Any]) -> str:
    """Return protected, graceful-quit, or review-only for a process instance."""
    name = _base_name(proc)
    if name in PROTECTED_APPLE_DAEMONS or name.startswith(PROTECTED_PREFIXES):
        return "protected"
    command = str(proc.get("command") or "")
    if name == "mail" and ".app/Contents/MacOS/Mail" in command:
        return "graceful-quit"
    if name == "shortcuts" and ".app/Contents/MacOS/Shortcuts" in command:
        return "graceful-quit"
    return "review-only"


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
    policy = process_action_policy(proc)
    try:
        result = subprocess.check_output(
            [
                "swift",
                os.path.join(core.BASE_DIR, "prompt.swift"),
                _display(proc),
                proc.get("command") or proc.get("comm", ""),
                "false",
                "process",
                detail,
                str(snooze_hours),
                str(keep_days),
                policy,
            ],
            text=True,
        ).strip().upper()
    except (OSError, subprocess.CalledProcessError):
        return "QUIT"
    allowed = {"INVESTIGATE", "KEEP", "SNOOZE", "QUIT"}
    if policy == "graceful-quit":
        allowed.add("GRACEFUL_QUIT")
    return result if result in allowed else "QUIT"


def investigation_prompt(core: Any, proc: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    del core, config
    return "\n".join(
        [
            "Investigate this high-impact macOS process and help me decide what to do.",
            "",
            "Use rootless, bounded inspection only. Do not run privileged tracing, recursive cloud scans, throttling, or termination automatically.",
            "",
            "Please cover:",
            "1. What this process likely is.",
            "2. Why it may be using CPU or disk I/O now.",
            "3. Which rootless observations would distinguish the likely causes.",
            "4. Whether a user-initiated graceful quit is appropriate.",
            "5. Recommended next action.",
            "",
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
        core.open_codex_in_terminal(investigation_prompt(core, proc, config), core.process_cwd(proc))
        return "investigating"
    return "unchanged"


def run_process_audit(core: Any, config: dict[str, Any], prompt_budget: int | None = None) -> tuple[bool, int]:
    limit = int(config.get("process_max_prompts", core.DEFAULT_MAX_PROMPTS))
    if prompt_budget is not None:
        limit = min(limit, max(0, int(prompt_budget)))
    if limit <= 0:
        return True, 0
    candidates = get_candidate_processes(config)
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
