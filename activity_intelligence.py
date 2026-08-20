#!/usr/bin/env python3
"""Aggregate activity/resource events and run Codex only for repeated patterns."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from idle_config import APP_SUPPORT_DIR, load_config

DIM = 96
DB_PATH = Path(APP_SUPPORT_DIR) / "activity-intelligence.sqlite3"
REPORT_PATH = Path(APP_SUPPORT_DIR) / "activity-intelligence-latest.md"
RESOURCE_STATE_PATH = Path(APP_SUPPORT_DIR) / "resource-monitor-state.json"
APP_USAGE_PATH = Path(APP_SUPPORT_DIR) / "app_usage.json"
LOCK_PATH = Path(APP_SUPPORT_DIR) / "activity-intelligence.lock"
LOG_PATH = Path(os.path.expanduser("~/Library/Logs/IdleMaintenance.activity-intelligence.log"))
DEFAULTS = {
    "activity_intelligence_enabled": True,
    "activity_intelligence_min_pattern_events": 3,
    "activity_intelligence_similarity_threshold": 0.72,
    "activity_intelligence_lookback_days": 7,
    "activity_intelligence_retention_days": 30,
    "activity_intelligence_max_events": 2000,
    "activity_intelligence_diagnosis_cooldown_hours": 24,
    "activity_intelligence_diagnosis_similarity_threshold": 0.84,
    "activity_intelligence_max_diagnoses_per_cycle": 1,
    "activity_intelligence_context_minutes": 20,
    "activity_intelligence_llm_timeout_seconds": 90,
    "activity_intelligence_llm_command": [
        "codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--sandbox", "read-only", "--skip-git-repo-check", "--config",
        'web_search="disabled"', "--",
    ],
}
TOKENS = re.compile(r"[a-z0-9_.:/+-]+", re.I)


def now_epoch() -> float:
    return time.time()


def setting(config: dict[str, Any], key: str) -> Any:
    return config.get(key, DEFAULTS[key])


def embed_text(text: str, dimensions: int = DIM) -> list[float]:
    """Deterministic local feature vector; no embedding service or upload."""
    vector = [0.0] * max(8, int(dimensions))
    for token in (x.lower() for x in TOKENS.findall(text or "") if len(x) > 1):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % len(vector)
        vector[index] += (-1.0 if digest[4] & 1 else 1.0) * (1 + min(len(token), 24) / 24)
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right)) if left and len(left) == len(right) else 0.0


def centroid(vectors: Iterable[Sequence[float]]) -> list[float]:
    rows = [list(row) for row in vectors]
    if not rows:
        return [0.0] * DIM
    result = [sum(row[i] for row in rows) / len(rows) for i in range(len(rows[0]))]
    norm = math.sqrt(sum(x * x for x in result))
    return [x / norm for x in result] if norm else result


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _atomic_write(path: Path, text: str) -> None:
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


def app_usage_context(path: Path, timestamp: float, window_seconds: float) -> list[str]:
    usage = _read_json(path, {})
    if not isinstance(usage, dict):
        return []
    nearby = []
    for app_path, observed in usage.items():
        distance = abs(timestamp - _float(observed, -1))
        if 0 <= distance <= window_seconds:
            name = os.path.basename(str(app_path).rstrip("/")) or "unknown"
            nearby.append((distance, name[:-4] if name.endswith(".app") else name))
    result = []
    for _, name in sorted(nearby, key=lambda item: (item[0], item[1].lower())):
        if name not in result:
            result.append(name)
        if len(result) == 4:
            break
    return result


def incident_event(incident: dict[str, Any], *, app_usage_path: Path = APP_USAGE_PATH,
                   context_minutes: float = 20) -> dict[str, Any] | None:
    incident_id = str(incident.get("id") or "").strip()
    if not incident_id:
        return None
    at = _float(incident.get("started_at"), now_epoch())
    triage = incident.get("triage") if isinstance(incident.get("triage"), dict) else {}
    known = incident.get("known_process") if isinstance(incident.get("known_process"), dict) else {}
    process = str(incident.get("process") or "process")
    process_key = str(incident.get("process_key") or "")
    group = str(known.get("recurrence_group") or "")
    apps = app_usage_context(app_usage_path, at, max(0.0, context_minutes) * 60)
    total, write = _float(incident.get("peak_total_mib_s")), _float(incident.get("peak_write_mib_s"))
    parts = [
        f"resource spike process {process}",
        f"process-key {process_key}" if process_key and not group else "",
        f"group {group}" if group else "",
        f"role {known.get('role')}" if known.get("role") else "",
        f"triage {triage.get('classification')} {triage.get('decision')}" if triage else "",
        f"peak-total {round(total / 5) * 5:.0f} MiB/s",
        f"peak-write {round(write / 5) * 5:.0f} MiB/s",
        "recurrent" if incident.get("recurrence") else "isolated",
        f"foreground {' '.join(apps)}" if apps else "",
    ]
    payload = {
        "incident_id": incident_id, "process": process, "process_key": process_key,
        "recurrence_group": group, "known_role": known.get("role"),
        "default_action": known.get("default_action"),
        "triage": {k: triage.get(k) for k in ("classification", "decision", "reason") if triage.get(k) is not None},
        "peak_total_mib_s": total, "peak_write_mib_s": write,
        "system_mib_s": _float(incident.get("system_mib_s")),
        "recurrence": bool(incident.get("recurrence")), "nearby_apps": apps,
        "status": incident.get("status"),
    }
    return {"event_id": f"idle-maintenance:{incident_id}", "timestamp": at,
            "source": "idle-maintenance", "kind": "resource-spike",
            "summary": " | ".join(x for x in parts if x), "payload": payload}


class VectorEventStore:
    """Bounded SQLite ledger with local vectors and cosine lookup."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
              event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, source TEXT NOT NULL,
              kind TEXT NOT NULL, summary TEXT NOT NULL, vector_json TEXT NOT NULL,
              payload_json TEXT NOT NULL, reviewed_at REAL);
            CREATE INDEX IF NOT EXISTS idx_event_lookup ON events(kind, reviewed_at, timestamp);
            CREATE TABLE IF NOT EXISTS diagnoses(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              trigger_event_id TEXT NOT NULL, centroid_json TEXT NOT NULL,
              event_ids_json TEXT NOT NULL, response TEXT NOT NULL, model TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_diagnosis_time ON diagnoses(created_at);
        """)
        self.connection.commit()

    def __enter__(self) -> "VectorEventStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"], "timestamp": float(row["timestamp"]),
            "source": row["source"], "kind": row["kind"], "summary": row["summary"],
            "vector": [float(x) for x in json.loads(row["vector_json"])],
            "payload": json.loads(row["payload_json"]), "reviewed_at": row["reviewed_at"],
        }

    def add_event(self, event: dict[str, Any]) -> bool:
        event_id, summary = str(event.get("event_id") or ""), str(event.get("summary") or "").strip()
        if not event_id or not summary:
            raise ValueError("event_id and summary are required")
        vector = event.get("vector") if isinstance(event.get("vector"), list) else embed_text(summary)
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,NULL)",
            (event_id, _float(event.get("timestamp"), now_epoch()), str(event.get("source") or "external"),
             str(event.get("kind") or "activity"), summary, json.dumps(vector),
             json.dumps(event.get("payload") if isinstance(event.get("payload"), dict) else {}, sort_keys=True)))
        self.connection.commit()
        return cursor.rowcount > 0

    def unreviewed_spikes(self) -> list[dict[str, Any]]:
        return [self._row(row) for row in self.connection.execute(
            "SELECT * FROM events WHERE kind='resource-spike' AND reviewed_at IS NULL ORDER BY timestamp LIMIT 100")]

    def similar_events(self, event: dict[str, Any], *, since: float, threshold: float) -> list[tuple[float, dict[str, Any]]]:
        matches = []
        for row in self.connection.execute(
            "SELECT * FROM events WHERE kind=? AND timestamp>=? AND event_id!=? ORDER BY timestamp DESC LIMIT 500",
            (event["kind"], float(since), event["event_id"])):
            candidate = self._row(row)
            score = cosine_similarity(event["vector"], candidate["vector"])
            if score >= threshold:
                matches.append((score, candidate))
        return sorted(matches, key=lambda item: (item[0], item[1]["timestamp"]), reverse=True)

    def context_events(self, timestamps: Sequence[float], window: float) -> list[dict[str, Any]]:
        if not timestamps or window <= 0:
            return []
        return [self._row(row) for row in self.connection.execute(
            "SELECT * FROM events WHERE source!='idle-maintenance' AND timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT 100",
            (min(timestamps) - window, max(timestamps) + window))]

    def mark_reviewed(self, event_id: str, at: float) -> None:
        self.connection.execute("UPDATE events SET reviewed_at=? WHERE event_id=?", (float(at), event_id))
        self.connection.commit()

    def add_diagnosis(self, *, created_at: float, trigger_event_id: str,
                      pattern_vector: Sequence[float], event_ids: Sequence[str],
                      response: str, model: str = "codex") -> None:
        self.connection.execute("INSERT INTO diagnoses(created_at,trigger_event_id,centroid_json,event_ids_json,response,model) VALUES(?,?,?,?,?,?)",
            (float(created_at), trigger_event_id, json.dumps(list(pattern_vector)),
             json.dumps(list(event_ids)), response, model))
        self.connection.commit()

    def has_recent_similar_diagnosis(self, vector: Sequence[float], *, since: float, threshold: float) -> bool:
        for row in self.connection.execute("SELECT centroid_json FROM diagnoses WHERE created_at>=? ORDER BY created_at DESC LIMIT 100", (float(since),)):
            if cosine_similarity(vector, json.loads(row["centroid_json"])) >= threshold:
                return True
        return False

    def latest_diagnosis(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM diagnoses ORDER BY created_at DESC LIMIT 1").fetchone()
        return None if row is None else {"id": row["id"], "created_at": row["created_at"],
            "trigger_event_id": row["trigger_event_id"], "event_ids": json.loads(row["event_ids_json"]),
            "response": row["response"], "model": row["model"]}

    def counts(self) -> dict[str, int]:
        events = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        diagnoses = self.connection.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]
        pending = self.connection.execute("SELECT COUNT(*) FROM events WHERE kind='resource-spike' AND reviewed_at IS NULL").fetchone()[0]
        return {"events": int(events), "stored_diagnoses": int(diagnoses), "pending_spikes": int(pending)}

    def prune(self, *, now: float, retention_days: float, max_events: int) -> None:
        cutoff = now - max(1.0, retention_days) * 86400
        self.connection.execute("DELETE FROM events WHERE timestamp<?", (cutoff,))
        excess = self.connection.execute("SELECT event_id FROM events ORDER BY timestamp DESC LIMIT -1 OFFSET ?", (max(1, max_events),)).fetchall()
        self.connection.executemany("DELETE FROM events WHERE event_id=?", [(row["event_id"],) for row in excess])
        self.connection.execute("DELETE FROM diagnoses WHERE created_at<?", (cutoff,))
        self.connection.commit()


@dataclass(frozen=True)
class DiagnosisResult:
    ok: bool
    text: str = ""
    error: str = ""
    model: str = "codex"


class CodexDiagnoser:
    def __init__(self, config: dict[str, Any], *, command_runner: Callable[..., Any] = subprocess.run) -> None:
        raw = setting(config, "activity_intelligence_llm_command")
        self.command = shlex.split(raw) if isinstance(raw, str) else [str(x) for x in raw] if isinstance(raw, list) else []
        self.timeout = max(5.0, float(setting(config, "activity_intelligence_llm_timeout_seconds")))
        self.command_runner = command_runner

    def diagnose(self, prompt: str) -> DiagnosisResult:
        if not self.command:
            return DiagnosisResult(False, error="LLM command is disabled")
        try:
            result = self.command_runner([*self.command, prompt], capture_output=True, text=True,
                                         timeout=self.timeout, check=False, cwd="/")
        except (OSError, subprocess.TimeoutExpired) as error:
            return DiagnosisResult(False, error=str(error))
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            return DiagnosisResult(False, error=detail[-1000:])
        text = (result.stdout or "").strip()
        return DiagnosisResult(bool(text), text=text[:8000], error="" if text else "Codex returned no diagnosis")


def build_diagnosis_prompt(pattern: Sequence[dict[str, Any]], context: Sequence[dict[str, Any]]) -> str:
    lines = [
        "Diagnose a repeated macOS activity/resource pattern from bounded local observations.",
        "Do not execute commands, change settings, kill processes, use the network, or infer physical-disk attribution.",
        "Explain the whole pattern, not only the latest spike. Return concise plain text with likely cause, evidence, safest remedy, and one verification step.",
        f"Pattern contains {len(pattern)} semantically similar events:",
    ]
    for event in sorted(pattern, key=lambda x: x["timestamp"])[-8:]:
        lines.append(f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(event['timestamp']))} [{event['source']}] {event['summary']}")
        payload = event.get("payload", {})
        if payload.get("default_action"):
            lines.append(f"  deterministic guidance: {payload['default_action']}")
        triage = payload.get("triage") if isinstance(payload.get("triage"), dict) else {}
        if triage.get("reason"):
            lines.append(f"  triage: {triage['reason']}")
    if context:
        lines.append("Nearby activity context:")
        for event in context[-12:]:
            lines.append(f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(event['timestamp']))} [{event['source']}/{event['kind']}] {event['summary']}")
    lines.append("Recommend only user-reviewable, reversible actions; for protected services, inspect the upstream workload rather than terminating the daemon.")
    return "\n".join(lines)


def notify_user(title: str, message: str) -> None:
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run\n'
    try:
        subprocess.run(["osascript", "-", title, message], input=script, capture_output=True,
                       text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def sync_resource_incidents(store: VectorEventStore, *, state_path: Path = RESOURCE_STATE_PATH,
                            app_usage_path: Path = APP_USAGE_PATH, context_minutes: float = 20) -> int:
    state = _read_json(state_path, {})
    incidents = state.get("incidents") if isinstance(state, dict) else []
    added = 0
    for incident in incidents if isinstance(incidents, list) else []:
        event = incident_event(incident, app_usage_path=app_usage_path, context_minutes=context_minutes) if isinstance(incident, dict) else None
        added += int(bool(event and store.add_event(event)))
    return added


def sync_app_usage(store: VectorEventStore, *, app_usage_path: Path = APP_USAGE_PATH) -> int:
    usage = _read_json(app_usage_path, {})
    added = 0
    for app_path, observed in usage.items() if isinstance(usage, dict) else []:
        at = _float(observed, -1)
        if at < 0:
            continue
        name = os.path.basename(str(app_path).rstrip("/"))
        name = name[:-4] if name.endswith(".app") else name
        event_id = "activity-watcher:" + hashlib.sha256(f"{app_path}\0{at:.3f}".encode()).hexdigest()[:24]
        added += int(store.add_event({"event_id": event_id, "timestamp": at, "source": "activity-watcher",
                                     "kind": "foreground-activity", "summary": f"foreground app {name}", "payload": {"app": name}}))
    return added


def record_external_event(*, source: str, kind: str, summary: str, payload: dict[str, Any] | None = None,
                          timestamp: float | None = None, event_id: str | None = None, db_path: Path = DB_PATH) -> str:
    at = now_epoch() if timestamp is None else float(timestamp)
    event_id = event_id or f"{source}:" + hashlib.sha256(f"{source}\0{kind}\0{at:.6f}\0{summary}".encode()).hexdigest()[:24]
    with VectorEventStore(db_path) as store:
        store.add_event({"event_id": event_id, "timestamp": at, "source": source, "kind": kind,
                         "summary": summary, "payload": payload or {}})
    return event_id


def install_codex_event_hook(core: Any, *, db_path: Path = DB_PATH) -> None:
    original = getattr(core, "open_codex_in_terminal", None)
    if not callable(original) or getattr(original, "_activity_intelligence_wrapped", False):
        return

    def wrapped(prompt_text: str, cwd: str = "/") -> Any:
        try:
            lines = [x.strip() for x in str(prompt_text).splitlines() if x.strip()]
            selected = [x for x in lines if x.startswith(("- Command:", "- Reason:", "Known macOS role:", "Default handling:"))][:5] or lines[:4]
            record_external_event(source="codex", kind="investigation",
                                  summary=("codex investigation | " + " | ".join(selected))[:900],
                                  payload={"origin": "idle-maintenance"}, db_path=db_path)
        except Exception:
            pass
        return original(prompt_text, cwd)

    wrapped._activity_intelligence_wrapped = True  # type: ignore[attr-defined]
    core.open_codex_in_terminal = wrapped


def write_report(path: Path, result: DiagnosisResult, pattern: Sequence[dict[str, Any]], created_at: float) -> None:
    evidence = "\n".join(f"- {event['summary']}" for event in pattern[-8:])
    _atomic_write(path, f"# Idle Maintenance activity intelligence\n\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_at))}\n\n## Diagnosis\n\n{result.text.strip()}\n\n## Pattern evidence\n\n{evidence}\n")


def run_cycle(config: dict[str, Any] | None = None, *, db_path: Path = DB_PATH,
              resource_state_path: Path = RESOURCE_STATE_PATH, app_usage_path: Path = APP_USAGE_PATH,
              report_path: Path = REPORT_PATH, diagnoser: Any | None = None,
              notify_fn: Callable[[str, str], None] = notify_user,
              now_fn: Callable[[], float] = now_epoch) -> dict[str, Any]:
    cfg = config or load_config(os.path.dirname(__file__))
    if not bool(setting(cfg, "activity_intelligence_enabled")):
        return {"enabled": False, "diagnoses": 0, "events_added": 0}
    now = float(now_fn())
    lookback = max(1.0, float(setting(cfg, "activity_intelligence_lookback_days"))) * 86400
    minimum = max(2, int(setting(cfg, "activity_intelligence_min_pattern_events")))
    similarity = min(1.0, max(0.0, float(setting(cfg, "activity_intelligence_similarity_threshold"))))
    diagnosis_similarity = min(1.0, max(0.0, float(setting(cfg, "activity_intelligence_diagnosis_similarity_threshold"))))
    cooldown = max(0.0, float(setting(cfg, "activity_intelligence_diagnosis_cooldown_hours"))) * 3600
    max_diagnoses = max(0, int(setting(cfg, "activity_intelligence_max_diagnoses_per_cycle")))
    context_window = max(0.0, float(setting(cfg, "activity_intelligence_context_minutes"))) * 60
    engine, diagnoses, added, errors = diagnoser or CodexDiagnoser(cfg), 0, 0, []
    with VectorEventStore(db_path) as store:
        added += sync_app_usage(store, app_usage_path=app_usage_path)
        added += sync_resource_incidents(store, state_path=resource_state_path,
                                         app_usage_path=app_usage_path, context_minutes=context_window / 60)
        for event in store.unreviewed_spikes():
            matches = store.similar_events(event, since=event["timestamp"] - lookback, threshold=similarity)
            pattern = sorted({x["event_id"]: x for x in [event] + [m[1] for m in matches]}.values(), key=lambda x: x["timestamp"])
            if len(pattern) < minimum:
                store.mark_reviewed(event["event_id"], now)
                continue
            vector = centroid(x["vector"] for x in pattern)
            if store.has_recent_similar_diagnosis(vector, since=now - cooldown, threshold=diagnosis_similarity):
                store.mark_reviewed(event["event_id"], now)
                continue
            if diagnoses >= max_diagnoses:
                break
            context = store.context_events([x["timestamp"] for x in pattern], context_window)
            result = engine.diagnose(build_diagnosis_prompt(pattern, context))
            if not isinstance(result, DiagnosisResult):
                result = DiagnosisResult(bool(result), text=str(result or ""))
            store.mark_reviewed(event["event_id"], now)
            if not result.ok:
                errors.append(result.error or "diagnosis failed")
                continue
            store.add_diagnosis(created_at=now, trigger_event_id=event["event_id"], pattern_vector=vector,
                                event_ids=[x["event_id"] for x in pattern], response=result.text, model=result.model)
            write_report(report_path, result, pattern, now)
            notify_fn("Idle Maintenance pattern diagnosis", f"{len(pattern)} similar events: {' '.join(result.text.split())[:360]}")
            diagnoses += 1
        store.prune(now=now, retention_days=float(setting(cfg, "activity_intelligence_retention_days")),
                    max_events=int(setting(cfg, "activity_intelligence_max_events")))
        counts = store.counts()
    return {"enabled": True, "events_added": added, "diagnoses": diagnoses, "errors": errors, **counts}


def launch_cycle(config: dict[str, Any] | None = None, *, base_dir: str | None = None) -> bool:
    cfg = config or load_config(base_dir or os.path.dirname(__file__))
    if not bool(setting(cfg, "activity_intelligence_enabled")):
        return False
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "process"], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False


def status(db_path: Path = DB_PATH) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"events": 0, "stored_diagnoses": 0, "pending_spikes": 0, "latest": None}
    with VectorEventStore(db_path) as store:
        return {**store.counts(), "latest": store.latest_diagnosis()}


def _lock() -> Any | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return None
    handle.seek(0); handle.truncate(); handle.write(f"{os.getpid()}\n"); handle.flush()
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("process")
    sub.add_parser("status")
    record = sub.add_parser("record")
    for name in ("source", "kind", "summary"):
        record.add_argument(f"--{name}", required=True)
    record.add_argument("--timestamp", type=float)
    record.add_argument("--payload-json", default="{}")
    args = parser.parse_args(argv)
    if (args.command or "process") == "process":
        lock = _lock()
        if lock is None:
            print(json.dumps({"enabled": True, "skipped": "already-running"}))
            return 0
        try:
            print(json.dumps(run_cycle(), sort_keys=True))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN); lock.close()
        return 0
    if args.command == "status":
        print(json.dumps(status(), indent=2, sort_keys=True))
        return 0
    try:
        payload = json.loads(args.payload_json)
    except json.JSONDecodeError as error:
        parser.error(f"invalid --payload-json: {error}")
    if not isinstance(payload, dict):
        parser.error("--payload-json must decode to an object")
    print(record_external_event(source=args.source, kind=args.kind, summary=args.summary,
                                payload=payload, timestamp=args.timestamp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
