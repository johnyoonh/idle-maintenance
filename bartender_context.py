"""Read-only return status and a bounded publisher for the Bartender controller.

This module does not run maintenance, sample HID input, launch applications,
read prompts/audio, or edit preferences. Run with the controller's Python.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

SCHEMA = 1
LIMIT = 1024 * 1024
CHANNELS = frozenset(("wispr", "meeting", "ai_openai", "ai_cursor", "ai_antigravity", "ai_copilot"))
HOME_STATE = Path.home() / "Library/Application Support"
DEFAULT_MONITOR = HOME_STATE / "idle-maintenance/resource-monitor-state.json"
DEFAULT_CONTROLLER = Path.home() / ".config/bartender/bartender.py"
DEFAULT_ROOT = HOME_STATE / "bartender-context"


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh(stamp, now, maximum):
    return finite(stamp) and stamp > 0 and 0 <= now - stamp < maximum


def private_json(path):
    """Read one bounded, regular, user-owned file without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > LIMIT:
            raise ValueError("Unsafe monitor state")
        data = handle.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("Oversized monitor state")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Monitor state must be an object")
    return value


def snapshot(path=DEFAULT_MONITOR, *, now=None, window=120.0, stale=90.0):
    now = time.time() if now is None else now
    if not finite(now) or not finite(window) or not 0 < window <= 300 or not finite(stale) or not 0 < stale <= 300:
        raise ValueError("Invalid time bounds")
    result = {"schema": SCHEMA, "monitor_fresh": False, "idle": False,
              "remaining_seconds": 0.0, "reason": "unavailable", "pending": False, "sample_at": 0.0}
    try:
        state = private_json(Path(path))
    except (OSError, ValueError, UnicodeError):
        return result
    health = state.get("health")
    if type(state.get("schema_version")) is not int or state["schema_version"] != SCHEMA or not isinstance(health, dict):
        return dict(result, reason="schema")
    if not fresh(health.get("last_sample_at"), now, stale):
        return dict(result, reason="stale")
    result["monitor_fresh"] = True
    result["sample_at"] = health["last_sample_at"]
    if health.get("return_routing_enabled") is not True:
        return dict(result, reason="disabled")
    result["pending"] = state.get("return_pending") is True
    # A pending review has no timestamp and can remain queued for hours. It is
    # not a fresh return event. last_return_flow_at is persisted by the resident
    # monitor before its return callback; success or failure both merit status.
    event = state.get("last_return_flow_at")
    if not fresh(event, now, window) or event > health["last_sample_at"]:
        return dict(result, reason="no-recent-return")
    return dict(result, idle=True, remaining_seconds=window - (now - event), reason="return-flow")



def pending_transition(previous, view, now, window=120.0):
    """Consume a fresh false->true monitor edge, never synthesize one at startup."""
    previous = previous if isinstance(previous, dict) else {}
    current_fresh = view["monitor_fresh"] and view["reason"] != "disabled"
    continuous = (previous.get("schema") == SCHEMA and previous.get("fresh") is True
                  and fresh(previous.get("updated_at"), now, 45)
                  and finite(previous.get("sample_at"))
                  and view["sample_at"] >= previous["sample_at"])
    edge = previous.get("edge_at", 0.0) if continuous else 0.0
    if (continuous and current_fresh and previous.get("pending") is False and view["pending"]
            and view["sample_at"] > previous["sample_at"]):
        edge = view["sample_at"]
    if current_fresh and fresh(edge, now, window):
        remaining = window - (now - edge)
        if not view["idle"] or remaining > view["remaining_seconds"]:
            view = dict(view, idle=True, remaining_seconds=remaining, reason="return-pending-edge")
    checkpoint = {"schema": SCHEMA, "updated_at": now, "sample_at": view["sample_at"],
                  "fresh": current_fresh, "pending": view["pending"], "edge_at": edge}
    return view, checkpoint


def validate_request(request, now):
    if not isinstance(request, dict) or set(request) != {"schema", "at", "meeting", "events"}:
        raise ValueError("Invalid request shape")
    if type(request["schema"]) is not int or request["schema"] != SCHEMA or not fresh(request["at"], now, 15):
        raise ValueError("Stale request")
    if request["events"] == {}:
        request["events"] = []
    if type(request["meeting"]) is not bool or not isinstance(request["events"], list) or len(request["events"]) > 64:
        raise ValueError("Invalid signals")
    seen = set()
    for event in request["events"]:
        if not isinstance(event, dict) or set(event) != {"signal", "source", "expires_at"}:
            raise ValueError("Invalid event shape")
        name, source, expires = event["signal"], event["source"], event["expires_at"]
        if not isinstance(name, str) or name not in CHANNELS or not isinstance(source, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", source):
            raise ValueError("Invalid channel or opaque source ID")
        if not finite(expires) or expires < 0 or expires > request["at"] + 300:
            raise ValueError("Invalid event expiry")
        key = (name, source)
        if key in seen:
            raise ValueError("Duplicate producer")
        seen.add(key)
    return request


def load_controller(path):
    path = Path(path).expanduser().resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Controller must be a user-owned, non-writable-by-others file")
    spec = importlib.util.spec_from_file_location("bartender_controller_bridge", path)
    if spec is None or spec.loader is None:
        raise ValueError("Controller cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "emit", None)) or not (CHANNELS | {"idle"}).issubset(set(getattr(module, "SIGNALS", ()))):
        raise ValueError("Update the Bartender controller: idle lease support is required")
    return module


def publish(request, controller, root, monitor=DEFAULT_MONITOR, *, now=None, window=120.0, stale=90.0):
    now = time.time() if now is None else now
    validate_request(request, now)  # Validate the entire batch before any write.
    if not (CHANNELS | {"idle"}).issubset(set(controller.SIGNALS)):
        raise ValueError("Incompatible controller")
    view = snapshot(monitor, now=now, window=window, stale=stale)
    checkpoint_path = Path(root) / "idle-return-checkpoint.json"
    view, checkpoint = pending_transition(controller.read_json(checkpoint_path), view, now, window)
    controller.private_dir(root)
    controller.write_json(checkpoint_path, checkpoint)
    # One producer cannot clear another producer's lease. All bridge identities
    # are fixed prefixes or opaque caller IDs; no titles/paths are persisted.
    controller.emit(root, "meeting", "hammerspoon.zoom", request["meeting"], 30, now=request["at"])
    controller.emit(root, "idle", "idle-maintenance.return", view["idle"],
                    min(30.0, view["remaining_seconds"]) if view["idle"] else 30.0, now=now)
    for event in request["events"]:
        remaining = event["expires_at"] - now
        controller.emit(root, event["signal"], "hammerspoon.event." + event["source"], remaining > 0,
                        min(30.0, remaining) if remaining > 0 else 30.0, now=now)
    return {"schema": SCHEMA, "published": True, "idle": view, "event_count": len(request["events"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "publish"))
    parser.add_argument("--monitor-state", type=Path, default=DEFAULT_MONITOR)
    parser.add_argument("--controller", type=Path, default=DEFAULT_CONTROLLER)
    parser.add_argument("--controller-state", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--window", type=float, default=120.0)
    parser.add_argument("--stale", type=float, default=90.0)
    args = parser.parse_args(argv)
    try:
        options = {"window": args.window, "stale": args.stale}
        if args.command == "snapshot":
            output = snapshot(args.monitor_state, **options)
        else:
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536:
                raise ValueError("Oversized request")
            request = json.loads(raw)
            validate_request(request, time.time())
            output = publish(request, load_controller(args.controller), args.controller_state,
                             args.monitor_state, **options)
        print(json.dumps(output, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        # Never echo request content, command output, paths, or exception text.
        print(json.dumps({"schema": SCHEMA, "published": False, "reason": "bridge-unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
