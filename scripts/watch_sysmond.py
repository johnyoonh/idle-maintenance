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
import stat
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


def sample_directory(path: str, owner_uid: int, owner_gid: int) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner_uid):
        raise RuntimeError(f"sample directory must be a real directory owned by root or the invoking user: {path}")
    if info.st_uid != owner_uid or info.st_gid != owner_gid:
        os.chown(path, owner_uid, owner_gid)
    os.chmod(path, 0o700)
    return path


def capture(pid: int, seconds: float, directory: str, owner_uid: int, owner_gid: int) -> tuple[str, str]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fd, sample_path = tempfile.mkstemp(prefix=f"sysmond_{stamp}_", suffix=".sample.txt", dir=directory)
    os.close(fd)
    # Preserve each private sample: when no peer name is found, the full report
    # is the evidence needed to improve the detector rather than a discarded file.
    os.chmod(sample_path, 0o600)
    try:
        run("/usr/bin/sample", str(pid), str(seconds), "-file", sample_path, timeout=seconds + 20)
        with open(sample_path, encoding="utf-8", errors="replace") as handle:
            report = handle.read()
        return sample_path, report
    finally:
        os.chown(sample_path, owner_uid, owner_gid)


def watch(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        print("Run with sudo so sample can inspect sysmond: sudo ./scripts/watch_sysmond.py", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("This watcher requires macOS.", file=sys.stderr)
        return 2

    print(
        f"Watching sysmond (threshold {args.threshold:g}% for {args.confirmations} checks, "
        f"every {args.interval:g}s); samples kept in {args.sample_dir} for the invoking user; Ctrl-C stops.",
        flush=True,
    )
    owner_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    owner_gid = int(os.environ.get("SUDO_GID", os.getgid()))
    try:
        args.sample_dir = sample_directory(args.sample_dir, owner_uid, owner_gid)
    except OSError as exc:
        print(f"cannot prepare private sample directory: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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
                    sample_path, report = capture(pid, args.sample_seconds, args.sample_dir, owner_uid, owner_gid)
                    peer_pids = sorted({int(value) for value in PEER_RE.findall(report)})
                    print(f"sampled sysmond pid={pid}; peer queues={len(peer_pids)}; sample={sample_path}", flush=True)
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
    parser.add_argument(
        "--sample-dir",
        default=os.path.join(tempfile.gettempdir(), "sysmond-watch"),
        help="private directory, owned by the invoking user, for retained sample reports (default: /tmp/sysmond-watch)",
    )
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
