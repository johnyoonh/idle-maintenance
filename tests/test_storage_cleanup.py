import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from storage_cleanup import DAY, StorageCleaner, main


class StorageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.now = time.time()
        self.runner = Mock()
        self.runner.return_value.returncode = 1
        self.runner.return_value.stderr = ""
        self.runner.return_value.stdout = ""

    def tearDown(self):
        self.temp.cleanup()

    def age(self, path: Path, days: int):
        timestamp = self.now - days * DAY
        os.utime(path, (timestamp, timestamp))

    def cleaner(self, free=500 * 1024**3, **overrides):
        config = {
            "cache_retention_days": 30,
            "log_retention_days": 30,
            "trash_retention_days": 30,
            "xcode_derived_data_retention_days": 14,
            "xcode_archive_retention_days": 90,
            "delete_screenpipe": True,
            "run_package_cleaners": False,
            "minimum_free_gb": 0,
            "cleanup_pressure_headroom_gb": 0,
            "state_path": "~/state.json",
            **overrides,
        }
        usage = lambda _: SimpleNamespace(total=1000 * 1024**3, used=0, free=free)
        return StorageCleaner(
            config,
            home=self.home,
            now=self.now,
            command_runner=self.runner,
            disk_usage_provider=usage,
        )

    def test_removes_only_aged_cache_children(self):
        root = self.home / "Library/Caches"
        old = root / "old-app"
        recent = root / "recent-app"
        old.mkdir(parents=True)
        recent.mkdir()
        (old / "cache.bin").write_bytes(b"old")
        (recent / "cache.bin").write_bytes(b"recent")
        self.age(old / "cache.bin", 31)
        self.age(old, 31)
        self.age(recent, 29)
        self.cleaner().run()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_dry_run_reports_without_deleting(self):
        old = self.home / ".Trash/old"
        old.mkdir(parents=True)
        old_file = old / "old.txt"
        old_file.write_text("old")
        self.age(old_file, 31)
        self.age(old, 31)
        cleaner = self.cleaner()
        cleaner.dry_run = True
        report = cleaner.run()
        self.assertTrue(old.exists())
        self.assertIn(str(old_file.resolve()), report.removed)

    def test_deletes_screenpipe_store_when_not_running(self):
        store = self.home / ".screenpipe/data"
        store.mkdir(parents=True)
        (store / "recording.mp4").write_bytes(b"data")
        self.cleaner().run()
        self.assertFalse((self.home / ".screenpipe").exists())

    def test_preserves_screenpipe_store_while_process_is_running(self):
        store = self.home / ".screenpipe/data"
        store.mkdir(parents=True)
        self.runner.return_value.returncode = 0
        report = self.cleaner().run()
        self.assertTrue(store.exists())
        self.assertTrue(any("Screenpipe is running" in item for item in report.skipped))

    def test_refuses_path_outside_approved_root(self):
        outside = self.home.parent / "outside-storage-cleanup-test"
        outside.mkdir(exist_ok=True)
        cleaner = self.cleaner()
        cleaner._remove(outside, self.home / "Library/Caches")
        self.assertTrue(outside.exists())
        self.assertTrue(any("unsafe path" in item for item in cleaner.report.failures))
        outside.rmdir()

    def test_preserves_aisess_cache_even_when_aged(self):
        aisess = self.home / ".cache/aisess"
        aisess.mkdir(parents=True)
        database = aisess / "sessions.db"
        database.write_bytes(b"important")
        self.age(database, 365)
        cleaner = self.cleaner(protected_paths=["~/.cache/aisess"])
        report = cleaner.run()
        self.assertTrue(database.exists())
        self.assertTrue(any("protected path" in item for item in report.skipped))

    def test_caps_failure_details_but_counts_all_failures(self):
        cleaner = self.cleaner()
        for number in range(105):
            cleaner._record_failure(f"failure {number}")
        self.assertEqual(cleaner.report.failure_count, 105)
        self.assertEqual(len(cleaner.report.failures), 100)

    def test_package_cleaners_are_deferred_when_space_is_healthy_and_not_due(self):
        cleaner = self.cleaner(
            run_package_cleaners=True,
            package_cleanup_interval_days=7,
        )
        cleaner.state["package_cleaners_at"] = self.now
        with patch("storage_cleanup.shutil.which", return_value="/bin/true"):
            report = cleaner.run()
        self.assertFalse(any(command.startswith("brew") for command in report.commands))
        self.assertTrue(any("package cleaners deferred" in item for item in report.skipped))

    def test_package_cleaners_run_under_space_pressure(self):
        cleaner = self.cleaner(
            free=50 * 1024**3,
            run_package_cleaners=True,
            minimum_free_gb=100,
            cleanup_pressure_headroom_gb=25,
        )
        with patch("storage_cleanup.shutil.which", return_value="/bin/true"):
            cleaner.run()
        self.assertTrue(any(command.startswith("brew") for command in cleaner.report.commands))


if __name__ == "__main__":
    unittest.main()
