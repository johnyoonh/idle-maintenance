#!/usr/bin/env python3
"""Watch sysmond for sustained high CPU and identify its live XPC peers.

Run with sudo so `sample` can inspect sysmond:
    sudo ./scripts/watch_sysmond.py

This is a foreground watcher. Press Ctrl-C to stop it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
import time


PEER_RE = re.compile(r"com\.apple\.sysmond\.peer\[(\d+)\]")


def run(*args: str, timeout: float = 10) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{args[0]} failed: {detail}")
    return result.stdout


def sysmond_pid() -> int | None:
    pids = run("/usr/bin/pgrep", "-x", "sysmond").split()
    return int(pids[0]) if pids else None


def cpu_percent(pid: int) -> float | None:
    try:
        value = run("/bin/ps", "-p", str(pid), "-o", "%cpu=").strip()
        return float(value) if value else None
    except (RuntimeError, ValueError):
        return None


def process_info(pid: int) -> str:
    try:
        row = run("/bin/ps", "-p", str(pid), "-o", "pid=,ppid=,user=,etime=,%cpu=,comm=").strip()
        return row or "exited before mapping"
    except RuntimeError:
        return "exited before mapping"


def capture(pid: int, seconds: float) -> str:
    # Keep diagnostic data private and ephemeral; print only extracted peer metadata.
    with tempfile.NamedTemporaryFile(prefix="sysmond-watch-", suffix=".sample", delete=False) as handle:
        sample_path = handle.name
    try:
        run("/usr/bin/sample", str(pid), str(seconds), "-file", sample_path, timeout=seconds + 20)
        with open(sample_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(sample_path)
        except FileNotFoundError:
            pass


def watch(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        print("Run with sudo so sample can inspect sysmond: sudo ./scripts/watch_sysmond.py", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("This watcher requires macOS.", file=sys.stderr)
        return 2

    print(
        f"Watching sysmond (threshold {args.threshold:g}% for {args.confirmations} checks, "
        f"every {args.interval:g}s); Ctrl-C stops.",
        flush=True,
    )
    consecutive = 0
    next_capture = 0.0
    while True:
        pid = sysmond_pid()
        current = cpu_percent(pid) if pid else None
        if current is None:
            consecutive = 0
            print("sysmond not found or CPU reading unavailable", flush=True)
        else:
            now = time.monotonic()
            consecutive = consecutive + 1 if current >= args.threshold else 0
            print(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} sysmond pid={pid} cpu={current:.1f}%", flush=True)
            if consecutive >= args.confirmations and now >= next_capture:
                try:
                    report = capture(pid, args.sample_seconds)
                    peer_pids = sorted({int(value) for value in PEER_RE.findall(report)})
                    print(f"sampled sysmond pid={pid}; peer queues={len(peer_pids)}", flush=True)
                    if not peer_pids:
                        print("  no sysmond.peer[PID] queue found in sample", flush=True)
                    for peer_pid in peer_pids:
                        print(f"  peer pid={peer_pid}: {process_info(peer_pid)}", flush=True)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    print(f"sample failed: {exc}", file=sys.stderr, flush=True)
                next_capture = time.monotonic() + args.cooldown
                consecutive = 0
        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=20, help="trigger threshold in ps CPU percent (default: 20)")
    parser.add_argument("--interval", type=float, default=5, help="seconds between lightweight checks (default: 5)")
    parser.add_argument("--confirmations", type=int, default=2, help="consecutive high readings before sampling (default: 2)")
    parser.add_argument("--sample-seconds", type=float, default=3, help="sample duration (default: 3)")
    parser.add_argument("--cooldown", type=float, default=30, help="minimum seconds between samples (default: 30)")
    args = parser.parse_args()
    if args.threshold <= 0 or args.interval <= 0 or args.confirmations < 1 or args.sample_seconds <= 0 or args.cooldown < 0:
        parser.error("threshold, interval, and sample duration must be positive; confirmations >= 1; cooldown >= 0")
    try:
        return watch(args)
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
