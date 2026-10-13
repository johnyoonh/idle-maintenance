"""Read-only, privacy-preserving activity probe for standalone Antigravity.

Only fresh local RUNNING trajectories without an approval wait grant activity.
Loopback JSON RPC is private implementation detail; its payloads and process
arguments must never be logged. This module emits only aggregate state.
"""

from datetime import datetime
import json
import math
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request


BINARY = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
MAX_PROCESSES = 4
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT = 0.75
MAX_AGE_SECONDS = 900


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


def _result(state, count=0):
    return {"active": count > 0, "state": state, "count": count}


def _ephemeral_ports(command, identities):
    matches = []
    for line in identities:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1] == command:
            matches.append(parts[0])
    if len(matches) != 1:
        raise ValueError("ambiguous process")
    listeners = subprocess.run(
        ["lsof", "-nP", "-a", "-p", matches[0], "-iTCP", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=0.75, check=True,
    )
    ports = sorted({int(value) for value in re.findall(
        r"TCP\s+(?:127\.0\.0\.1|\[::1\]):(\d+)\s+\(LISTEN\)", listeners.stdout,
    )})
    if not ports or len(ports) > 4 or any(not 1 <= port <= 65535 for port in ports):
        raise ValueError("invalid listeners")
    return ports


def _active(summary, now):
    if not isinstance(summary, dict):
        raise ValueError("invalid summary")
    for name in ("killed", "interrupted", "cloud"):
        if name in summary and type(summary[name]) is not bool:
            raise ValueError("invalid flag")
    waiting = summary.get("waitingSteps", [])
    if not isinstance(waiting, list):
        raise ValueError("invalid waiting steps")
    status = summary.get("status")
    running = (type(status) is int and status == 2) or status == "CASCADE_RUN_STATUS_RUNNING"
    if not running or waiting or any(summary.get(name, False) for name in ("killed", "interrupted", "cloud")):
        return False
    timestamp = summary.get("lastModifiedTime")
    if not isinstance(timestamp, str):
        raise ValueError("invalid timestamp")
    changed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if changed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    age = now - changed.timestamp()
    return -5 <= age <= MAX_AGE_SECONDS


def probe(commands=None, now=None):
    """Return active/state/count, never credentials, identifiers, or errors.

    ``commands`` may reuse the daemon's raw command snapshot. Failure or partial
    coverage is unknown and never grants wakefulness. No endpoint is retried.
    """
    try:
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            return _result("unknown")
        if commands is None:
            snapshot = subprocess.run(
                ["ps", "-axo", "command="], capture_output=True, text=True,
                timeout=2, check=True,
            )
            commands = snapshot.stdout.splitlines()
        if not isinstance(commands, (list, tuple)) or any(not isinstance(command, str) for command in commands):
            return _result("unknown")
        servers = [command.strip() for command in commands if command.strip().startswith(BINARY + " ")]
        if not servers:
            return _result("absent")
        if len(servers) > MAX_PROCESSES:
            return _result("unknown")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )
        count = 0
        requests_left = 4
        identities = None
        for command in servers:
            ports = re.findall(r"(?:^|\s)--https_server_port(?:=|\s+)(\S+)", command)
            tokens = re.findall(r"(?:^|\s)--csrf_token(?:=|\s+)(\S+)", command)
            if len(ports) != 1 or len(tokens) != 1 or not ports[0].isdigit() or not 0 <= int(ports[0]) <= 65535:
                return _result("unknown")
            if ports[0] == "0":
                if identities is None:
                    identities = subprocess.run(
                        ["ps", "-axo", "pid=,command="], capture_output=True,
                        text=True, timeout=2, check=True,
                    ).stdout.splitlines()
                candidates = _ephemeral_ports(command, identities)
            else:
                candidates = [int(ports[0])]
            payload = None
            for port in candidates:
                if requests_left == 0:
                    return _result("unknown")
                requests_left -= 1
                request = urllib.request.Request(
                    f"https://127.0.0.1:{port}/exa.language_server_pb."
                    "LanguageServerService/GetAllCascadeTrajectories",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                             "X-Codeium-Csrf-Token": tokens[0]},
                    method="POST",
                )
                try:
                    with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                        payload = response.read(MAX_RESPONSE_BYTES + 1)
                    break
                except urllib.error.URLError:
                    continue  # A discovered listener may serve a different protocol.
            if payload is None:
                return _result("unknown")
            if len(payload) > MAX_RESPONSE_BYTES:
                return _result("unknown")
            data = json.loads(payload)
            if not isinstance(data, dict):
                return _result("unknown")
            if data and "trajectorySummaries" not in data:
                return _result("unknown")
            summaries = data.get("trajectorySummaries", {})
            if not isinstance(summaries, dict):
                return _result("unknown")
            count += sum(_active(summary, now) for summary in summaries.values())
        return _result("observed", count)
    except Exception:
        # Errors may contain private response bodies or credential-bearing URLs.
        return _result("unknown")
