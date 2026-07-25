#!/usr/bin/env python3
"""Pressure-aware wrapper around the conservative storage cleanup core."""
from __future__ import annotations
import argparse, fcntl, json, os, shutil, sys, time
from pathlib import Path
import storage_cleanup_core as _core
from idle_config import atomic_write_json, disk_busy_status, load_config, read_json_file

DAY = _core.DAY; Report = _core.Report; DEFAULT_LOG = _core.DEFAULT_LOG; DEFAULT_LOCK = _core.DEFAULT_LOCK
write_report = _core.write_report; path_size = _core.path_size

class StorageCleaner(_core.StorageCleaner):
    def __init__(self, config, *, home=None, dry_run=None, now=None, command_runner=_core.subprocess.run, disk_usage_provider=shutil.disk_usage):
        super().__init__(config, home=home, dry_run=dry_run, now=now, command_runner=command_runner)
        self.disk_usage_provider = disk_usage_provider
        state = str(config.get("state_path", "~/Library/Application Support/idle-maintenance/storage-cleanup-state.json"))
        if state.startswith("~/"): state = str(self.home / state[2:])
        self.state_path = Path(os.path.expanduser(state))
        loaded = read_json_file(self.state_path)
        self.state = loaded if isinstance(loaded, dict) else {}

    def _due(self, key, days):
        return self.now - float(self.state.get(key, 0) or 0) >= max(1, int(days)) * DAY

    def _mark(self, key):
        self.state[key] = int(self.now)

    def run(self):
        usage = self.disk_usage_provider(self.home); free_before = int(usage.free)
        minimum = int(float(self.config.get("minimum_free_gb", 100)) * 1024**3)
        headroom = int(float(self.config.get("cleanup_pressure_headroom_gb", 25)) * 1024**3)
        pressure = free_before < minimum + headroom
        self.report.free_bytes_before = free_before
        self.prune_aged_files(self.home / "Library/Caches", int(self.config.get("cache_retention_days", 30)))
        self.prune_aged_files(self.home / ".cache", int(self.config.get("cache_retention_days", 30)))
        self.prune_aged_files(self.home / "Library/Logs", int(self.config.get("log_retention_days", 30)))
        self.prune_aged_files(self.home / ".Trash", int(self.config.get("trash_retention_days", 30)))
        self.prune_aged_children(self.home / "Library/Developer/Xcode/DerivedData", int(self.config.get("xcode_derived_data_retention_days", 14)))
        self.prune_aged_children(self.home / "Library/Developer/Xcode/Archives", int(self.config.get("xcode_archive_retention_days", 90)))
        if bool(self.config.get("delete_screenpipe", True)): self.delete_screenpipe()
        self.run_supported_command("unavailable simulators", ["xcrun", "simctl", "delete", "unavailable"])
        package_due = self._due("package_cleaners_at", self.config.get("package_cleanup_interval_days", 7))
        if bool(self.config.get("run_package_cleaners", True)) and (pressure or package_due):
            self.run_supported_command("Homebrew", ["brew", "cleanup", "-s"])
            self.run_supported_command("uv", ["uv", "cache", "prune"])
            self.run_supported_command("Go build cache", ["go", "clean", "-cache"])
            self._mark("package_cleaners_at")
        else:
            self.report.skipped.append("package cleaners deferred: free space healthy and interval not due")
        scan_due = self._due("large_path_scan_at", self.config.get("large_path_scan_interval_days", 7))
        if pressure or scan_due:
            self.collect_largest_paths(); self._mark("large_path_scan_at")
        else:
            self.report.skipped.append("large path scan deferred: free space healthy and interval not due")
        free_after = int(self.disk_usage_provider(self.home).free); self.report.free_bytes_after = free_after
        if free_after < minimum: self._record_failure(f"free space is {free_after / 1024**3:.1f} GB; configured minimum is {minimum / 1024**3:.1f} GB")
        if not self.dry_run and not atomic_write_json(self.state_path, self.state): self._record_failure(f"could not write cleanup state: {self.state_path}")
        return self.report

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ignore-disk-busy", action="store_true")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    return parser.parse_args(argv)

def main(argv=None):
    args = parse_args(argv); root = load_config(Path(__file__).resolve().parent)
    config = root.get("storage_cleanup", {})
    if not isinstance(config, dict) or not config.get("enabled", True): return 0
    if config.get("defer_when_disk_busy", True) and not args.ignore_disk_busy and not args.dry_run:
        status = disk_busy_status(root)
        if status.get("busy"):
            print(f"storage cleanup deferred: disk activity {status['mib_per_second']:.1f} MiB/s exceeds {status['threshold_mib_per_second']:.1f} MiB/s", file=sys.stderr)
            return 75
    DEFAULT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_LOCK.open("w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: print("storage cleanup is already running", file=sys.stderr); return 0
        report = StorageCleaner(config, dry_run=True if args.dry_run else None).run(); write_report(report, args.log)
    if args.json or args.dry_run: print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
