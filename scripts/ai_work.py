"""Read bounded AI lifecycle evidence without retaining conversational content."""
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

MAX_BYTES = 8 * 1024 * 1024
MAX_AGE = 900
CURSOR_AGE = 60
START = {"task_started"}
END = {"task_complete", "turn_aborted", "turn_cancelled", "shutdown_complete"}
PAUSE = {"exec_approval_request", "apply_patch_approval_request", "elicitation_request", "request_user_input"}


def lifecycle(item, state):
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")
    call = payload.get("call_id")
    if not isinstance(call, str) or not call:
        call = "__unmatched_approval__"
    if item.get("type") == "event_msg":
        if kind in START | END:
            state.update(event=kind, waiting=set())
        elif kind in PAUSE:
            state["waiting"].add(call)
        elif kind in ("exec_command_begin", "patch_apply_begin"):
            state["waiting"].discard(call)
    elif item.get("type") == "response_item":
        if kind in ("function_call", "custom_tool_call"):
            name = payload.get("name", "")
            arguments = payload.get("arguments") or payload.get("input") or ""
            if (isinstance(name, str) and name.split(".")[-1] == "request_user_input"
                    or isinstance(arguments, str) and re.search(
                        r'''["']?sandbox_permissions["']?\s*:\s*["']require_escalated["']''', arguments)):
                state["waiting"].add(call)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            state["waiting"].discard(call)


def run(argv, timeout=5):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout


def boot_id():
    return run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], timeout=1).strip()


def process_snapshot():
    raw = run(["/bin/ps", "-axo", "pid=,ppid=,lstart=,command="], timeout=2)
    result = {}
    for line in raw.splitlines():
        fields = line.split(None, 7)
        if len(fields) == 8 and fields[0].isdigit() and fields[1].isdigit():
            result[int(fields[0])] = {"ppid": int(fields[1]), "birth": " ".join(fields[2:7]), "command": fields[7]}
    return result


def is_provider(command, kind):
    if not command:
        return False
    if kind == "cursor":
        return command.startswith("/Applications/Cursor.app/Contents/")
    if kind == "claude":
        executable = command.split(None, 1)[0]
        return (Path(executable).name == "claude"
                or "/.local/share/claude/versions/" in executable)
    return False


def hook_owner(processes, kind, start):
    seen = set()
    for _ in range(16):
        if start in seen or start not in processes:
            break
        seen.add(start)
        proc = processes[start]
        if is_provider(proc["command"], kind):
            return {"pid": start, "birth": proc["birth"]}
        start = proc["ppid"]
    return None


def open_codex_files(raw):
    files = []
    command = None
    for line in raw.splitlines():
        if line.startswith("p"):
            command = None
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and command == "codex":
            path = Path(line[1:])
            if path.name.startswith("rollout-") and path.suffix == ".jsonl":
                files.append(path)
    return list(dict.fromkeys(files))


class CodexProbe:
    def __init__(self, root):
        self.root = root.resolve()
        self.cache = {}

    def inspect(self, paths, now=None):
        now = time.time() if now is None else now
        active = 0
        keep = {}
        for path in paths[:64]:
            path = path.resolve()
            if self.root not in path.parents:
                continue
            try:
                stat = path.stat()
                previous = self.cache.get(path)
                identity = (stat.st_dev, stat.st_ino)
                offset, state = 0, {"event": None, "waiting": set()}
                if (previous and previous[0] == identity and previous[1] <= stat.st_size
                        and not (previous[1] == stat.st_size and previous[3] != stat.st_mtime_ns)):
                    offset, state = previous[1:3]
                if stat.st_size - offset > MAX_BYTES:
                    offset, state = stat.st_size - MAX_BYTES, {"event": None, "waiting": set()}
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(MAX_BYTES)
                # A partially written trailing record is re-read next time.
                consumed = data.rfind(b"\n") + 1
                for line in data[:consumed].splitlines():
                    try:
                        item = json.loads(line)
                        lifecycle(item, state)
                    except (ValueError, AttributeError, TypeError):
                        continue
                keep[path] = (identity, offset + consumed, state, stat.st_mtime_ns)
                if state["event"] in START and not state["waiting"] and -5 <= now - stat.st_mtime <= MAX_AGE:
                    active += 1
            except OSError:
                continue
        self.cache = keep
        return active

    def probe(self):
        try:
            raw = subprocess.run(["/usr/sbin/lsof", "-nP", "-c", "codex", "-Fpcn"],
                                 capture_output=True, text=True, timeout=3)
            if raw.returncode not in (0, 1):
                raise OSError("process inventory unavailable")
            return {"active": bool(count := self.inspect(open_codex_files(raw.stdout))),
                    "count": count, "state": "observed"}
        except (OSError, subprocess.SubprocessError):
            return {"active": False, "count": 0, "state": "unknown"}


def atomic_json(path, data):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class HookStore:
    def __init__(self, root, boot):
        self.root, self.boot = root, boot

    def record(self, kind, payload, owner, now=None):
        now = time.time() if now is None else now
        if kind not in ("claude", "cursor"):
            return
        session = payload.get("session_id") or payload.get("conversation_id")
        generation = payload.get("generation_id") if kind == "cursor" else "claude"
        event = payload.get("hook_event_name")
        states = ({"beforeSubmitPrompt": "pending", "afterAgentThought": "active",
                   "afterAgentResponse": "active", "preToolUse": "waiting",
                   "postToolUse": "active", "postToolUseFailure": "active", "stop": "ended", "sessionEnd": "ended"}
                  if kind == "cursor" else
                  {"UserPromptSubmit": "active", "PreToolUse": "active", "PostToolUse": "active",
                   "PostToolUseFailure": "active", "PermissionRequest": "waiting",
                   "Notification": "waiting", "Elicitation": "waiting", "PostToolBatch": "active",
                   "StopFailure": "ended", "Stop": "ended", "SessionEnd": "ended"})
        if kind == "claude" and event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
            states[event] = "waiting"
        if event not in states or owner is None and states[event] not in ("ended", "waiting"):
            return
        if not isinstance(session, str) or not 0 < len(session) <= 256:
            return
        session_hash = hashlib.sha256(session.encode()).hexdigest()
        if event not in ("SessionEnd", "sessionEnd") and (not isinstance(generation, str) or not 0 < len(generation) <= 256):
            return
        key = hashlib.sha256((kind + session_hash + str(generation)).encode()).hexdigest()
        events = self.root / "events"
        events.mkdir(parents=True, exist_ok=True)
        data = {"kind": kind, "session": session_hash, "pid": owner["pid"] if owner else None,
                "birth": owner["birth"] if owner else None, "boot": self.boot, "key": key,
                "event": event, "state": states[event], "updated": now,
                "tool": hashlib.sha256(str(payload["tool_use_id"]).encode()).hexdigest()
                if isinstance(payload.get("tool_use_id"), str) and 0 < len(payload["tool_use_id"]) <= 256 else "unknown"}
        # Persist first. A contended state lock cannot discard Stop/approval events.
        atomic_json(events / (uuid.uuid4().hex + ".json"), data)
        self.drain()

    def drain(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "hooks.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            # Terminal tombstones survive for a day to reject delayed progress.
            # Retire old records so the bounded reader cannot fill with history.
            for old in itertools.islice(self.root.glob("*.json"), 4096):
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            pending = list(itertools.islice((self.root / "events").glob("*.json"), 4097))
            if len(pending) > 4096:
                return False
            records = []
            for path in pending:
                if path.stat().st_size > 4096:
                    return False
                records.append((json.loads(path.read_text()), path))
            for data, source in sorted(records, key=lambda entry: entry[0]["updated"]):
                self.apply_event(data)
                source.unlink()
            return True

    def apply_event(self, data):
        if data["boot"] != self.boot:
            return
        event, kind, now = data["event"], data["kind"], data["updated"]
        cutoff_path = self.root / ("session-" + hashlib.sha256((kind + data["session"]).encode()).hexdigest() + ".json")
        cutoff = json.loads(cutoff_path.read_text()) if cutoff_path.exists() else {}
        if cutoff.get("boot") == self.boot and cutoff.get("updated", 0) >= now:
            return
        if event in ("SessionEnd", "sessionEnd"):
            atomic_json(cutoff_path, {"boot": self.boot, "updated": now})
            for path in itertools.islice(self.root.glob("*.json"), 4096):
                previous = json.loads(path.read_text())
                if (previous.get("session") == data["session"] and previous.get("kind") == kind
                        and previous.get("updated", 0) <= now):
                    previous.update(state="ended", updated=now)
                    atomic_json(path, previous)
            return
        if not re.fullmatch(r"[0-9a-f]{64}", data["key"]):
            raise ValueError("Invalid event identity")
        path = self.root / (data["key"] + ".json")
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("updated", 0) > now:
            return
        if previous.get("state") == "ended" and not (kind == "claude" and event == "UserPromptSubmit"):
            return
        waits = set(previous.get("waits", []))
        if event in ("UserPromptSubmit", "PostToolBatch") or data["state"] == "ended":
            waits.clear()
        elif data["state"] == "waiting":
            waits.add(data["tool"])
        elif event in ("PostToolUse", "PostToolUseFailure", "postToolUse", "postToolUseFailure"):
            if data["tool"] != "unknown":
                waits.discard(data["tool"])
        if waits:
            data["state"] = "waiting"
        data["waits"] = sorted(waits)
        if data["pid"] is None:
            data.update(pid=previous.get("pid"), birth=previous.get("birth"))
        atomic_json(path, data)

    def active(self, processes, now=None):
        now = time.time() if now is None else now
        result = {"claude": 0, "cursor": 0}
        if not self.drain():
            return result
        for path in itertools.islice(self.root.glob("*.json"), 4096):
            try:
                if path.stat().st_size > 4096:
                    continue
                data = json.loads(path.read_text())
                kind = data.get("kind")
                owner = processes.get(data.get("pid"), {})
                ttl = CURSOR_AGE if kind == "cursor" else MAX_AGE
                if (kind in result and data.get("state") == "active" and data.get("boot") == self.boot
                        and owner.get("birth") == data.get("birth")
                        and is_provider(owner.get("command", ""), kind)
                        and -5 <= now - data["updated"] <= ttl):
                    result[kind] += 1
            except (ValueError, AttributeError, TypeError, KeyError, OSError):
                continue
        return result
