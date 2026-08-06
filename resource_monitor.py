#!/usr/bin/env python3
"""Continuously detect sustained process I/O without claiming physical attribution."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from idle_config import APP_SUPPORT_DIR, atomic_write_json, load_config, sample_disk_activity
import process_identity as identity

MIB = 1024 * 1024
STATE_SCHEMA = 1
DEFAULT_STATE_PATH = Path(APP_SUPPORT_DIR) / "resource-monitor-state.json"
DEFAULT_HISTORY_PATH = Path(APP_SUPPORT_DIR) / "resource-monitor-history.jsonl"
DEFAULT_LOCK_PATH = Path(APP_SUPPORT_DIR) / "resource-monitor.lock"
ATTRIBUTION_NOTE = (
    "I/O charged to this process during the sampled window; "
    "this is not definitive physical-disk attribution."
)


def now_epoch() -> float:
    return time.time()


def process_instance_id(proc: dict[str, Any]) -> str:
    start = proc.get("start_abstime")
    if start is None:
        start = proc.get("start_time", "unknown")
    payload = "\0".join(
        [
            str(proc.get("uid", "")),
            str(proc.get("pid", "")),
            str(start),
            str(proc.get("fingerprint") or identity.fingerprint(proc.get("command", ""))),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def rolling_delta(previous: dict[str, Any], current: dict[str, Any], seconds: float) -> dict[str, Any] | None:
    """Return one monotonic process-I/O interval, rejecting reuse and counter resets."""
    if seconds <= 0 or not identity.same(previous, current):
        return None
    required = ("io_read_bytes", "io_write_bytes")
    if any(key not in previous or key not in current for key in required):
        return None
    read_bytes = int(current["io_read_bytes"]) - int(previous["io_read_bytes"])
    write_bytes = int(current["io_write_bytes"]) - int(previous["io_write_bytes"])
    if read_bytes < 0 or write_bytes < 0:
        return None
    total_bytes = read_bytes + write_bytes
    return {
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "total_bytes": total_bytes,
        "read_mib_s": read_bytes / MIB / seconds,
        "write_mib_s": write_bytes / MIB / seconds,
        "total_mib_s": total_bytes / MIB / seconds,
    }


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "health": {},
        "incidents": [],
        "active": {},
        "notifications": {},
        "pending_prompts": [],
        "windows": {},
        "idle_armed": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA:
        return _default_state()
    base = _default_state()
    base.update(value)
    for key, expected in (("incidents", list), ("active", dict), ("notifications", dict),
                          ("pending_prompts", list), ("windows", dict), ("health", dict)):
        if not isinstance(base.get(key), expected):
            base[key] = expected()
    return base


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def append_bounded_jsonl(path: Path, record: dict[str, Any], limit: int) -> None:
    """Append a history record while retaining only the newest bounded set."""
    rows: list[str] = []
    try:
        rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        pass
    rows.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    rows = rows[-max(1, int(limit)):]
    _atomic_write_text(path, "\n".join(rows) + "\n")


def read_idle_seconds(command_runner: Callable[..., Any] | None = None) -> float:
    """Read HID idle time without privileges; return zero when unavailable."""
    import subprocess

    runner = command_runner or subprocess.run
    try:
        result = runner(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout or "")
    return int(match.group(1)) / 1_000_000_000 if match else 0.0


class ResourceMonitor:
    """State machine for aggregate-gated, sustained per-process I/O incidents."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        history_path: Path = DEFAULT_HISTORY_PATH,
        now_fn: Callable[[], float] = now_epoch,
        notify_fn: Callable[[str, str], None] | None = None,
        prompt_fn: Callable[[dict[str, Any], dict[str, Any]], str] | None = None,
        identity_reader: Callable[[int], dict[str, Any] | None] = identity.read,
    ) -> None:
        self.config = config or load_config(os.path.dirname(__file__))
        self.state_path = Path(state_path)
        self.history_path = Path(history_path)
        self.now_fn = now_fn
        self.notify_fn = notify_fn or self._notify_default
        self.prompt_fn = prompt_fn or self._prompt_default
        self.identity_reader = identity_reader
        self.state = _read_json(self.state_path)

    @property
    def interval_seconds(self) -> float:
        return max(1.0, float(self.config.get("resource_monitor_interval_seconds", 10)))

    def _notify_default(self, title: str, message: str) -> None:
        from maintenance_core import notify_user

        notify_user(title, message)

    def _prompt_default(self, proc: dict[str, Any], incident: dict[str, Any]) -> str:
        import maintenance_interactive as core
        from process_review import handle_process_action, prompt_process

        action = prompt_process(
            core,
            proc,
            float(self.config.get("process_snooze_hours", 24)),
            1,
        )
        handle_process_action(core, proc, action, self.config)
        return action

    def _persist(self) -> None:
        if not atomic_write_json(self.state_path, self.state):
            raise OSError(f"failed to atomically write {self.state_path}")

    def _history(self, event: str, incident: dict[str, Any], **fields: Any) -> None:
        record = {
            "schema_version": STATE_SCHEMA,
            "event": event,
            "timestamp": self.now_fn(),
            "incident_id": incident["id"],
            "process_identity": incident["process_identity"],
            "pid": incident.get("pid"),
            "process": incident.get("process"),
            **fields,
        }
        append_bounded_jsonl(
            self.history_path,
            record,
            int(self.config.get("resource_monitor_history_limit", 500)),
        )

    def _incident_by_id(self, incident_id: str) -> dict[str, Any] | None:
        return next((item for item in self.state["incidents"] if item.get("id") == incident_id), None)

    def _prune(self, now: float) -> None:
        incident_limit = max(1, int(self.config.get("resource_monitor_incident_limit", 50)))
        self.state["incidents"] = sorted(
            [item for item in self.state["incidents"] if isinstance(item, dict)],
            key=lambda item: float(item.get("last_seen_at", item.get("started_at", 0)) or 0),
        )[-incident_limit:]
        valid_ids = {item.get("id") for item in self.state["incidents"]}
        self.state["active"] = {
            key: value for key, value in self.state["active"].items() if value in valid_ids
        }
        self.state["pending_prompts"] = [
            value for value in self.state["pending_prompts"] if value in valid_ids
        ][-incident_limit:]
        cooldown = float(self.config.get("resource_monitor_notification_cooldown_seconds", 6 * 3600))
        notifications = sorted(
            ((key, float(value)) for key, value in self.state["notifications"].items()),
            key=lambda item: item[1],
        )
        self.state["notifications"] = {
            key: value for key, value in notifications[-100:] if now - value <= cooldown * 2
        }
        self.state["windows"] = {
            key: list(value)[-2:] for key, value in self.state["windows"].items()
            if isinstance(value, list)
        }

    def _qualified(self, interval: dict[str, Any]) -> bool:
        total_limit = float(self.config.get("process_high_io_total_mib_per_second", 20))
        write_limit = float(self.config.get("process_high_io_write_mib_per_second", 10))
        return interval["total_mib_s"] >= total_limit or interval["write_mib_s"] >= write_limit

    def _open_incident(
        self,
        proc: dict[str, Any],
        instance: str,
        window: list[dict[str, Any]],
        system_rate: float,
        now: float,
    ) -> dict[str, Any]:
        recent_recovery = max(
            (
                float(item.get("ended_at", 0) or 0)
                for item in self.state["incidents"]
                if item.get("process_identity") == instance and item.get("ended_at")
            ),
            default=0.0,
        )
        recurrence_window = float(self.config.get("resource_monitor_recurrence_seconds", 30 * 60))
        recurrence = bool(recent_recovery and now - recent_recovery <= recurrence_window)
        incident_id = f"{int(now)}-{instance[:12]}"
        command = str(proc.get("command") or proc.get("comm") or "process")
        incident = {
            "id": incident_id,
            "process_identity": instance,
            "process_key": proc.get("process_key") or identity.key(proc),
            "process": os.path.basename(str(proc.get("comm") or command)) or "process",
            "pid": int(proc["pid"]),
            "started_at": now,
            "last_seen_at": now,
            "ended_at": None,
            "cool_samples": 0,
            "status": "active",
            "prompt_status": "immediate" if recurrence else "queued",
            "recurrence": recurrence,
            "system_mib_s": round(system_rate, 3),
            "peak_total_mib_s": round(max(item["total_mib_s"] for item in window), 3),
            "peak_write_mib_s": round(max(item["write_mib_s"] for item in window), 3),
            "window_bytes": int(sum(item["total_bytes"] for item in window)),
            "attribution": ATTRIBUTION_NOTE,
            "process_snapshot": {
                key: proc.get(key)
                for key in (
                    "pid", "ppid", "uid", "comm", "command", "fingerprint",
                    "start_abstime", "start_time", "process_key", "etime", "elapsed_seconds",
                )
            },
        }
        self.state["incidents"].append(incident)
        self.state["active"][instance] = incident_id
        if not recurrence:
            self.state["pending_prompts"].append(incident_id)
        self._history("opened", incident, recurrence=recurrence, attribution=ATTRIBUTION_NOTE)

        cooldown = float(self.config.get("resource_monitor_notification_cooldown_seconds", 6 * 3600))
        last_value = self.state["notifications"].get(instance)
        last_notified = float(last_value) if last_value is not None else None
        if last_notified is None or now - last_notified >= cooldown:
            self.notify_fn(
                "Idle Maintenance resource incident",
                f"{incident['process']} sustained {incident['peak_total_mib_s']:.1f} MiB/s. {ATTRIBUTION_NOTE}",
            )
            self.state["notifications"][instance] = now
            incident["notified_at"] = now
        if recurrence:
            self._deliver_prompt(incident, now)
        return incident

    def _deliver_prompt(self, incident: dict[str, Any], now: float) -> None:
        expected = incident.get("process_snapshot") or {}
        current = self.identity_reader(int(incident["pid"]))
        if not identity.same(expected, current):
            incident["prompt_status"] = "stale"
            incident["prompted_at"] = now
            self._history("prompt-skipped", incident, reason="process identity changed")
        else:
            try:
                action = self.prompt_fn(current, incident)
            except Exception as error:
                detail = str(error).strip() or type(error).__name__
                incident["prompt_status"] = "failed"
                incident["prompt_error"] = detail[:500]
                incident["prompted_at"] = now
                self.state["prompt_health"] = {
                    "last_error": detail[:500],
                    "last_error_at": now,
                }
                self._history("prompt-failed", incident, error=detail[:500])
            else:
                incident["prompt_status"] = "completed"
                incident["prompt_action"] = str(action)
                incident["prompted_at"] = now
                self.state["prompt_health"] = {
                    "last_error": "",
                    "last_success_at": now,
                }
                self._history("prompted", incident, action=str(action))
        self.state["pending_prompts"] = [
            value for value in self.state["pending_prompts"] if value != incident["id"]
        ]

    def _handle_idle_return(self, idle_seconds: float, now: float) -> None:
        idle_threshold = float(self.config.get("return_from_away_minutes", 15)) * 60
        if idle_seconds >= idle_threshold:
            self.state["idle_armed"] = True
            return
        if not self.state.get("idle_armed") or idle_seconds >= 60:
            return
        self.state["idle_armed"] = False
        for incident_id in list(self.state["pending_prompts"]):
            incident = self._incident_by_id(incident_id)
            if incident:
                self._deliver_prompt(incident, now)

    def observe(
        self,
        previous: dict[int, dict[str, Any]],
        current: dict[int, dict[str, Any]],
        system_status: dict[str, Any],
        *,
        seconds: float | None = None,
        idle_seconds: float = 0,
        now: float | None = None,
    ) -> None:
        """Consume one process interval and atomically persist monitor state."""
        observed_at = self.now_fn() if now is None else float(now)
        elapsed = self.interval_seconds if seconds is None else float(seconds)
        available = bool(system_status.get("available"))
        system_rate = float(system_status.get("mib_per_second") or 0)
        system_gate = float(self.config.get("system_disk_busy_mib_per_second", 50))
        aggregate_hot = available and system_rate >= system_gate
        hot_instances: set[str] = set()
        seen_instances: set[str] = set()

        for pid, proc in current.items():
            instance = process_instance_id(proc)
            seen_instances.add(instance)
            old = previous.get(pid)
            interval = rolling_delta(old, proc, elapsed) if old else None
            window = self.state["windows"].setdefault(instance, [])
            if interval is None:
                window.clear()
                continue
            interval["qualified"] = bool(aggregate_hot and self._qualified(interval))
            if aggregate_hot:
                window.append(interval)
                del window[:-2]
            else:
                window.clear()
            if interval["qualified"]:
                hot_instances.add(instance)

            active_id = self.state["active"].get(instance)
            if active_id:
                incident = self._incident_by_id(active_id)
                if incident:
                    incident["last_seen_at"] = observed_at
                    incident["peak_total_mib_s"] = max(
                        float(incident.get("peak_total_mib_s", 0)), interval["total_mib_s"]
                    )
                    incident["peak_write_mib_s"] = max(
                        float(incident.get("peak_write_mib_s", 0)), interval["write_mib_s"]
                    )
                    if interval["qualified"]:
                        incident["cool_samples"] = 0

            required = max(1, int(self.config.get("process_io_required_intervals", 2)))
            minimum = float(self.config.get("process_io_minimum_window_mib", 256)) * MIB
            qualifying = sum(bool(item.get("qualified")) for item in window)
            total_bytes = sum(int(item.get("total_bytes", 0)) for item in window)
            if (
                instance not in self.state["active"]
                and len(window) >= 2
                and qualifying >= required
                and total_bytes >= minimum
            ):
                self._open_incident(proc, instance, list(window), system_rate, observed_at)

        if available:
            recovery_samples = max(1, int(self.config.get("resource_monitor_recovery_cool_samples", 6)))
            for instance, incident_id in list(self.state["active"].items()):
                if instance in hot_instances:
                    continue
                incident = self._incident_by_id(incident_id)
                if not incident:
                    self.state["active"].pop(instance, None)
                    continue
                incident["cool_samples"] = int(incident.get("cool_samples", 0)) + 1
                if incident["cool_samples"] >= recovery_samples:
                    incident["status"] = "recovered"
                    incident["ended_at"] = observed_at
                    incident["last_seen_at"] = observed_at
                    self.state["active"].pop(instance, None)
                    self.state["windows"].pop(instance, None)
                    self._history("recovered", incident, cool_samples=recovery_samples)

        self._handle_idle_return(float(idle_seconds), observed_at)
        prompt_health = self.state.get("prompt_health") if isinstance(self.state.get("prompt_health"), dict) else {}
        self.state["health"] = {
            "pid": os.getpid(),
            "last_sample_at": observed_at,
            "sample_interval_seconds": self.interval_seconds,
            "last_system_mib_s": system_rate if available else None,
            "last_error": "" if available else str(system_status.get("error") or "iostat unavailable"),
            "last_prompt_error": str(prompt_health.get("last_error") or ""),
            "last_prompt_error_at": prompt_health.get("last_error_at"),
            "last_prompt_success_at": prompt_health.get("last_success_at"),
            "active_incidents": len(self.state["active"]),
            "pending_prompts": len(self.state["pending_prompts"]),
        }
        self._prune(observed_at)
        self._persist()


def acquire_single_instance(path: Path) -> Any | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def run_monitor(
    config: dict[str, Any] | None = None,
    *,
    once: bool = False,
    snapshot_provider: Callable[[], dict[int, dict[str, Any]]] | None = None,
    disk_provider: Callable[[float], dict[str, Any]] | None = None,
    idle_provider: Callable[[], float] = read_idle_seconds,
) -> int:
    cfg = config or load_config(os.path.dirname(__file__))
    lock_path = Path(os.path.expanduser(str(cfg.get("resource_monitor_lock_path", DEFAULT_LOCK_PATH))))
    lock = acquire_single_instance(lock_path)
    if lock is None:
        return 0
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    previous_handlers = {
        signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        monitor = ResourceMonitor(cfg)
        provider = snapshot_provider or (lambda: identity.snapshot(cfg))
        disk = disk_provider or (
            lambda seconds: sample_disk_activity(seconds, executable="/usr/sbin/iostat")
        )
        previous = provider()
        while not stop:
            status = disk(monitor.interval_seconds)
            current = provider()
            monitor.observe(
                previous,
                current,
                status,
                seconds=monitor.interval_seconds,
                idle_seconds=idle_provider(),
            )
            previous = current
            if once:
                break
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Collect one 10-second interval and exit.")
    args = parser.parse_args(argv)
    return run_monitor(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
