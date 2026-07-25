#!/usr/bin/env python3
"""Conservative, scheduled cleanup for regenerable local storage."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from idle_config import APP_SUPPORT_DIR, load_config

DAY = 86400
DEFAULT_LOG = Path.home() / "Library/Logs/idle-maintenance/storage-cleanup.jsonl"
DEFAULT_LOCK = Path(APP_SUPPORT_DIR) / "storage-cleanup.lock"


@dataclass
class Report:
    dry_run: bool
    started_at: int = field(default_factory=lambda: int(time.time()))
    reclaimed_bytes: int = 0
    removed_count: int = 0
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    failure_count: int = 0
    commands: list[str] = field(default_factory=list)
    largest_paths: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        result = vars(self).copy()
        result["finished_at"] = int(time.time())
        return result


def path_size(path: Path) -> int:
    try:
        if path.is_symlink() or path.is_file():
            stat = path.lstat()
            return getattr(stat, "st_blocks", 0) * 512 or stat.st_size
        root_stat = path.lstat()
        total = getattr(root_stat, "st_blocks", 0) * 512 or root_stat.st_size
        pending = [path]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        total += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    except OSError:
                        continue
        return total
    except (FileNotFoundError, OSError):
        return 0


class StorageCleaner:
    def __init__(
        self,
        config: dict[str, object],
        *,
        home: Path | None = None,
        dry_run: bool | None = None,
        now: float | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.home = (home or Path.home()).resolve()
        self.config = config
        self.dry_run = bool(config.get("dry_run", False) if dry_run is None else dry_run)
        self.now = now or time.time()
        self.run_command = command_runner
        self.report = Report(dry_run=self.dry_run)
        self._report_lock = threading.Lock()
        configured = config.get("protected_paths", [])
        self.protected_paths = {
            self._expand_home(str(path)) for path in configured if isinstance(path, str)
        }

    def _expand_home(self, value: str) -> Path:
        if value == "~":
            return self.home
        if value.startswith("~/"):
            return Path(os.path.abspath(self.home / value[2:]))
        return Path(os.path.abspath(os.path.expanduser(value)))

    def _is_protected(self, path: Path) -> bool:
        absolute = Path(os.path.abspath(path))
        return any(
            absolute == protected or absolute.is_relative_to(protected)
            for protected in self.protected_paths
        )

    def _approved(self, path: Path, root: Path) -> bool:
        try:
            path_abs = Path(os.path.abspath(path))
            root_abs = Path(os.path.abspath(root))
            return path_abs != root_abs and path_abs.is_relative_to(root_abs)
        except (OSError, ValueError):
            return False

    def _record_failure(self, message: str) -> None:
        with self._report_lock:
            self.report.failure_count += 1
            if len(self.report.failures) < 100:
                self.report.failures.append(message)

    def _remove(self, path: Path, root: Path) -> None:
        if self._is_protected(path):
            self.report.skipped.append(f"protected path: {path}")
            return
        if not self._approved(path, root):
            self._record_failure(f"refused unsafe path: {path}")
            return
        try:
            size = path_size(path)
            if not self.dry_run:
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.exists():
                    shutil.rmtree(path)
            with self._report_lock:
                self.report.reclaimed_bytes += size
                self.report.removed_count += 1
                if len(self.report.removed) < 100:
                    self.report.removed.append(str(path))
        except OSError as exc:
            self._record_failure(f"{path}: {exc}")

    def prune_aged_files(self, root: Path, days: int) -> None:
        if not root.exists() or root.is_symlink():
            return
        cutoff = self.now - max(days, 1) * DAY
        directories: list[Path] = []
        candidates: list[Path] = []
        for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            directories.append(current)
            for name in list(dirnames):
                path = current / name
                if self._is_protected(path):
                    self.report.skipped.append(f"protected path: {path}")
                    dirnames.remove(name)
                    continue
                if path.is_symlink():
                    try:
                        if path.lstat().st_mtime < cutoff:
                            candidates.append(path)
                    except OSError as exc:
                        self._record_failure(f"{path}: {exc}")
                    dirnames.remove(name)
            for name in filenames:
                path = current / name
                if self._is_protected(path):
                    self.report.skipped.append(f"protected path: {path}")
                    continue
                try:
                    if path.lstat().st_mtime < cutoff:
                        candidates.append(path)
                except OSError as exc:
                    self._record_failure(f"{path}: {exc}")
        workers = max(1, min(int(self.config.get("delete_workers", 8)), 16))
        if self.dry_run or workers == 1:
            for path in candidates:
                self._remove(path, root)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                batch_size = 5000
                for start in range(0, len(candidates), batch_size):
                    batch = candidates[start : start + batch_size]
                    list(pool.map(lambda path: self._remove(path, root), batch, chunksize=128))
        if not self.dry_run:
            for directory in reversed(directories):
                if directory != root:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

    def prune_aged_children(self, root: Path, days: int) -> None:
        if not root.exists() or root.is_symlink():
            return
        cutoff = self.now - max(days, 1) * DAY
        for path in root.iterdir():
            try:
                if self._is_protected(path):
                    self.report.skipped.append(f"protected path: {path}")
                    continue
                if path.lstat().st_mtime < cutoff:
                    self._remove(path, root)
            except OSError as exc:
                self._record_failure(f"{path}: {exc}")

    def delete_screenpipe(self) -> None:
        root = self.home / ".screenpipe"
        if not root.exists() and not root.is_symlink():
            return
        process = self.run_command(
            ["/usr/bin/pgrep", "-x", "screenpipe"], capture_output=True, text=True, check=False
        )
        if process.returncode == 0:
            self.report.skipped.append("Screenpipe is running; close it before deleting its store")
            return
        self._remove(root, self.home)

    def run_supported_command(self, label: str, command: list[str]) -> None:
        executable = shutil.which(command[0])
        if not executable:
            self.report.skipped.append(f"{label}: command unavailable")
            return
        display = " ".join(command)
        self.report.commands.append(display)
        if self.dry_run:
            return
        result = self.run_command(
            [executable, *command[1:]], capture_output=True, text=True, timeout=900, check=False
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().splitlines()
            self._record_failure(f"{label}: {detail[-1] if detail else 'failed'}")

    def collect_largest_paths(self) -> None:
        support = self.home / "Library/Application Support"
        candidates = [
            support / "Logos4",
            support / "Microsoft Edge",
            support / "Google",
            support / "Claude",
            support / "ChatGPT Wiki Ingest Chrome",
            support / "Cursor",
            support / "obsidian",
            self.home / "Library/Developer",
            self.home / "Library/Caches",
            self.home / ".local",
            self.home / "repos",
            self.home / ".gemini",
            self.home / ".bun",
            self.home / ".config",
        ]
        measured = sorted(((path_size(p), p) for p in candidates if p.exists()), reverse=True)
        self.report.largest_paths = [
            {"path": str(path), "bytes": size} for size, path in measured[:10]
        ]
        warning = int(float(self.config.get("large_path_warning_gb", 5)) * 1024**3)
        for size, path in measured:
            if size >= warning:
                self.report.skipped.append(
                    f"large durable path requires manual review: {path} ({size / 1024**3:.1f} GB)"
                )

    def run(self) -> Report:
        cache_days = int(self.config.get("cache_retention_days", 30))
        log_days = int(self.config.get("log_retention_days", 30))
        trash_days = int(self.config.get("trash_retention_days", 30))
        self.prune_aged_files(self.home / "Library/Caches", cache_days)
        self.prune_aged_files(self.home / ".cache", cache_days)
        self.prune_aged_files(self.home / "Library/Logs", log_days)
        self.prune_aged_files(self.home / ".Trash", trash_days)
        self.prune_aged_children(
            self.home / "Library/Developer/Xcode/DerivedData",
            int(self.config.get("xcode_derived_data_retention_days", 14)),
        )
        self.prune_aged_children(
            self.home / "Library/Developer/Xcode/Archives",
            int(self.config.get("xcode_archive_retention_days", 90)),
        )
        if bool(self.config.get("delete_screenpipe", True)):
            self.delete_screenpipe()
        self.run_supported_command("unavailable simulators", ["xcrun", "simctl", "delete", "unavailable"])
        if bool(self.config.get("run_package_cleaners", True)):
            self.run_supported_command("Homebrew", ["brew", "cleanup", "-s"])
            self.run_supported_command("uv", ["uv", "cache", "prune"])
            self.run_supported_command("Go build cache", ["go", "clean", "-cache"])
        self.collect_largest_paths()
        free = shutil.disk_usage(self.home).free
        minimum = int(float(self.config.get("minimum_free_gb", 100)) * 1024**3)
        if free < minimum:
            self._record_failure(
                f"free space is {free / 1024**3:.1f} GB; configured minimum is {minimum / 1024**3:.1f} GB"
            )
        return self.report


def write_report(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report.as_dict(), sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without deleting or running cleaners")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(__file__).resolve().parent).get("storage_cleanup", {})
    if not isinstance(config, dict) or not config.get("enabled", True):
        return 0
    DEFAULT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("storage cleanup is already running", file=sys.stderr)
            return 0
        report = StorageCleaner(config, dry_run=True if args.dry_run else None).run()
        write_report(report, args.log)
    if args.json or args.dry_run:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
