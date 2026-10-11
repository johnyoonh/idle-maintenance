#!/usr/bin/env python3
"""Own the global sleep override only for observable online AI work."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import threading
import time
import urllib.error
import urllib.request

from ai_work import (CodexProbe, HookStore, atomic_json, boot_id, hook_owner,
                     process_snapshot, run)
from antigravity_work import probe as antigravity_probe

INTERVAL = 15
GRACE = 60
LEASE = 90


def shared_clock():
    # macOS system Python 3.9 time.monotonic() has a process-relative origin.
    # CLOCK_MONOTONIC is boot-shared, so watchdog and controller agree.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class Policy:
    def __init__(self):
        self.last_eligible = None

    def decide(self, active, online, now=None):
        now = shared_clock() if now is None else now
        if active and online:
            self.last_eligible = now
            return True
        return self.last_eligible is not None and now - self.last_eligible < GRACE


class Power:
    def read(self):
        text = run(["/usr/bin/pmset", "-g"], timeout=3)
        match = re.search(r"^\s*SleepDisabled\s+([01])\s*$", text, re.MULTILINE)
        if match is None:
            raise OSError("Cannot verify global sleep override")
        return match[1] == "1"

    def apply(self, disabled):
        if self.read() == disabled:
            return
        run(["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", str(int(disabled))], timeout=5)
        if self.read() != disabled:
            raise OSError("Sleep override readback mismatch")


def valid_lease(data, boot, now=None):
    now = shared_clock() if now is None else now
    try:
        if (not isinstance(data, dict) or data.get("boot") != boot
                or type(data.get("disabled")) is not bool
                or type(data.get("pid")) is not int or data["pid"] <= 1
                or type(data.get("expires")) not in (int, float)
                or not math.isfinite(data["expires"])
                or not now < data["expires"] <= now + LEASE + 5):
            return False
        os.kill(data["pid"], 0)
        return True
    except (OSError, ValueError, OverflowError):
        return False


def read_json(path):
    try:
        if path.stat().st_size > 65536:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def internet_available():
    # No credentials, user data, inference request, or response body is sent/read.
    for url in ("https://api.openai.com/", "https://api.anthropic.com/"):
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # Auth/route errors still prove verified HTTPS connectivity.
        except (OSError, urllib.error.URLError):
            continue
    return False


class Sources:
    def __init__(self, home, boot):
        self.codex = CodexProbe(home / ".codex/sessions")
        self.hooks = HookStore(home / "Library/Application Support/idle-maintenance/ai-sleep/hooks", boot)

    def inspect(self):
        result = {"codex": self.codex.probe()}
        try:
            processes = process_snapshot()
            hooks = self.hooks.active(processes)
            result.update({kind: {"active": bool(count), "count": count, "state": "observed"}
                           for kind, count in hooks.items()})
            result["antigravity"] = antigravity_probe([p["command"] for p in processes.values()])
        except Exception:
            # Never expose process arguments, CSRF tokens, or hook inputs in errors.
            for kind in ("claude", "cursor", "antigravity"):
                result.setdefault(kind, {"active": False, "count": 0, "state": "unknown"})
        return result


def watchdog(root, boot, power):
    with (root / "power.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        lease = read_json(root / "lease.json")
        if not valid_lease(lease, boot) or not lease["disabled"]:
            power.apply(False)


def controller(root, boot, sources, power):
    with (root / "controller.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("AI sleep controller already running")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        policy, previous, failed = Policy(), None, False
        logging.info("AI sleep controller started")
        try:
            while not stop.is_set():
                try:
                    evidence = sources.inspect()
                    active = any(item["active"] for item in evidence.values())
                    online = internet_available() if active else False
                    desired = policy.decide(active, online)
                    with (root / "power.lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        power.apply(desired)
                        state = {"boot": boot, "pid": os.getpid(), "disabled": desired,
                                 "expires": shared_clock() + LEASE, "online": online,
                                 "sources": evidence}
                        atomic_json(root / "lease.json", state)
                    if desired != previous:
                        logging.info("Sleep override %s", "enabled for online AI work" if desired else "released")
                    previous = desired
                    failed = False
                except Exception:
                    # Do not renew the lease on an unsuccessful iteration. The
                    # independent watchdog clears stale overrides after a failure.
                    if not failed:
                        logging.error("AI sleep check failed; watchdog lease not renewed")
                    failed = True
                stop.wait(INTERVAL)
        finally:
            with (root / "power.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                power.apply(False)
                atomic_json(root / "lease.json", None)
            logging.info("AI sleep controller stopped; override released")


def hook(kind, root, boot):
    try:
        import sys
        ingress = time.time()
        data = sys.stdin.buffer.read(131073)
        if len(data) > 131072:
            return
        payload = json.loads(data)
        if not isinstance(payload, dict):
            return
        suppress = payload.get("hook_event_name") in ("Stop", "stop", "SessionEnd", "sessionEnd", "PermissionRequest", "Notification", "Elicitation", "StopFailure")
        suppress = suppress or kind == "claude" and payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion"
        suppress = suppress or kind == "cursor" and payload.get("hook_event_name") == "preToolUse"
        owner = None if suppress else hook_owner(process_snapshot(), kind, os.getppid())
        HookStore(root / "hooks", boot).record(kind, payload, owner, now=ingress)
    except Exception:
        pass  # A failed observer never blocks or changes the AI operation.


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Read job/connectivity/power state only")
    mode.add_argument("--watchdog", action="store_true", help="Release a stale controller override")
    mode.add_argument("--hook", choices=("claude", "cursor"), help="Consume allowlisted lifecycle metadata")
    args = parser.parse_args()
    home, boot = Path.home(), boot_id()
    root = home / "Library/Application Support/idle-maintenance/ai-sleep"
    if args.hook:
        hook(args.hook, root, boot)
        print("{}")
        return
    sources, power = Sources(home, boot), Power()
    if args.status:
        evidence = sources.inspect()
        active = any(item["active"] for item in evidence.values())
        print(json.dumps({"sources": evidence, "internet": internet_available() if active else None,
                          "sleep_disabled": power.read(),
                          "controller_lease_valid": valid_lease(read_json(root / "lease.json"), boot)}))
        return
    root.mkdir(parents=True, exist_ok=True)
    if args.watchdog:
        watchdog(root, boot, power)
    else:
        controller(root, boot, sources, power)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
