from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_leftovers
import maintenance_core
import maintenance_interactive
from idle_config import DEFAULT_CONFIG


def enabled_config(home: Path) -> dict:
    return {
        "app_cleanup": {
            "leftover_review": {
                "enabled": True,
                "mode": "conservative",
                "action": "quarantine",
                "max_item_size_mb": 1,
                "max_total_size_mb": 2,
                "quarantine_dir": str(home / "quarantine"),
                "ledger": str(home / "quarantine.jsonl"),
            }
        }
    }


class AppLeftoversTests(unittest.TestCase):
    def test_quarantine_is_disabled_by_default(self):
        policy = DEFAULT_CONFIG["app_cleanup"]["leftover_review"]

        self.assertFalse(policy["enabled"])
        self.assertFalse(app_leftovers.quarantine_enabled(DEFAULT_CONFIG))

    def test_default_prompt_does_not_scan_or_display_config_paths(self):
        with patch.object(app_leftovers, "discover_leftovers") as discover:
            detail = maintenance_interactive._leftover_review_detail(
                "/Applications/Example.app", DEFAULT_CONFIG
            )

        self.assertEqual(detail, "")
        discover.assert_not_called()

    def test_discovery_skips_a_candidate_that_contains_quarantine_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app_support = home / "Library/Application Support/Example"
            app_support.mkdir(parents=True)
            (app_support / "settings.json").write_text("{}", encoding="utf-8")
            (app_support / "Caches").mkdir()
            (app_support / "Caches/cache.bin").write_bytes(b"cache")
            config = enabled_config(home)
            config["app_cleanup"]["leftover_review"]["quarantine_dir"] = str(app_support / "quarantine")

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app", {"bundle_id": "com.example.app"}, config
                )

        self.assertEqual(leftovers, [])

    def test_discovery_excludes_symlinks_caches_and_over_limit_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app_support = home / "Library/Application Support/Example"
            app_support.mkdir(parents=True)
            (app_support / "settings.json").write_text("{}", encoding="utf-8")
            cache = home / "Library/Caches/Example"
            cache.mkdir(parents=True)
            (cache / "cache.bin").write_bytes(b"cache")
            large = home / ".config/example"
            large.mkdir(parents=True)
            (large / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
            target = home / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            (app_support / "linked.txt").symlink_to(target)

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    enabled_config(home),
                )

            by_name = {Path(item["path"]).name: item for item in leftovers}
            self.assertIn("Example", by_name)
            self.assertIn("example", by_name)
            self.assertFalse(by_name["example"]["eligible"])
            self.assertFalse(by_name["Example"]["eligible"])
            self.assertEqual(by_name["Example"]["excluded_reason"], "contains-excluded-content")
            self.assertNotIn("Caches", {Path(item["path"]).parent.name for item in leftovers})
            self.assertNotIn("linked.txt", {Path(item["path"]).name for item in leftovers})

    def test_discovery_rejects_symlinked_parent_that_resolves_outside_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            external = root / "external"
            home.mkdir()
            support = external / "Application Support/Example"
            support.mkdir(parents=True)
            (support / "settings.json").write_text("{}", encoding="utf-8")
            (home / "Library").symlink_to(external, target_is_directory=True)

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    enabled_config(home),
                )

        self.assertEqual(leftovers, [])

    def test_plist_discovery_does_not_match_another_apps_name_substring(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            preferences = home / "Library/Preferences"
            preferences.mkdir(parents=True)
            (preferences / "com.vendor.MailPlugin.plist").write_text("unrelated", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Mail.app",
                    {"bundle_id": "com.example.mail"},
                    enabled_config(home),
                )

        self.assertNotIn("com.vendor.MailPlugin.plist", {Path(item["path"]).name for item in leftovers})

    def test_unreadable_directory_is_ineligible_instead_of_under_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            candidate = home / "Library/Application Support/Example"
            candidate.mkdir(parents=True)
            (candidate / "settings.json").write_text("{}", encoding="utf-8")

            def unreadable_walk(_path, *, onerror, **_kwargs):
                onerror(PermissionError("fixture unreadable subdirectory"))
                return iter(())

            with (
                patch.dict(os.environ, {"HOME": str(home)}),
                patch.object(app_leftovers, "candidate_paths", return_value=[candidate]),
                patch.object(app_leftovers.os, "walk", side_effect=unreadable_walk),
            ):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app", {}, enabled_config(home)
                )

        self.assertEqual(len(leftovers), 1)
        self.assertFalse(leftovers[0]["eligible"])
        self.assertEqual(leftovers[0]["excluded_reason"], "unreadable-content")

    def test_prompt_detail_lists_paths_that_opted_in_quarantine_will_move(self):
        home = Path("/tmp/test-home")
        items = [
            {"path": "/tmp/test-home/Library/Application Support/Example", "size": 10, "eligible": True},
        ]
        with (
            patch.object(maintenance_interactive._core, "app_metadata", return_value={"bundle_id": "com.example"}),
            patch.object(app_leftovers, "discover_leftovers", return_value=items),
        ):
            detail = maintenance_interactive._leftover_review_detail(
                "/Applications/Example.app", enabled_config(home)
            )

        self.assertIn("Library/Application Support/Example", detail)
        self.assertIn("quarantine is enabled", detail.lower())

    def test_repeated_quarantines_use_distinct_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = enabled_config(home)
            source = home / "Library/Application Support/Example"
            results = []
            with patch.dict(os.environ, {"HOME": str(home)}), patch(
                "app_leftovers.time.strftime", return_value="2026-10-07"
            ):
                for index in range(3):
                    source.mkdir(parents=True)
                    (source / "settings.json").write_text(str(index), encoding="utf-8")
                    leftovers = app_leftovers.discover_leftovers(
                        "/Applications/Example.app", {"bundle_id": "com.example"}, config
                    )
                    entries = app_leftovers.quarantine_leftovers(
                        "/Applications/Example.app", {"bundle_id": "com.example"}, leftovers, config
                    )
                    results.extend(entries)

            paths = [Path(item["quarantine_path"]) for item in results]
            self.assertEqual(len(paths), 3)
            self.assertEqual(len(set(paths)), 3)
            self.assertEqual(sorted(path.joinpath("settings.json").read_text() for path in paths), ["0", "1", "2"])
            ledger = home / "quarantine.jsonl"
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 3)

    def test_ledger_failure_rolls_the_config_item_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "Library/Application Support/Example"
            source.mkdir(parents=True)
            (source / "settings.json").write_text("{}", encoding="utf-8")
            config = enabled_config(home)
            bad_ledger = home / "ledger-directory"
            bad_ledger.mkdir()
            config["app_cleanup"]["leftover_review"]["ledger"] = str(bad_ledger)

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app", {"bundle_id": "com.example"}, config
                )
                with self.assertRaises(IsADirectoryError):
                    app_leftovers.quarantine_leftovers(
                        "/Applications/Example.app", {"bundle_id": "com.example"}, leftovers, config
                    )

            self.assertTrue(source.is_dir())
            self.assertTrue((source / "settings.json").exists())

    def test_quarantine_rechecks_size_before_moving_changed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "Library/Application Support/Example"
            source.mkdir(parents=True)
            (source / "settings.json").write_text("{}", encoding="utf-8")
            config = enabled_config(home)
            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = app_leftovers.discover_leftovers(
                    "/Applications/Example.app", {"bundle_id": "com.example"}, config
                )
                (source / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
                entries = app_leftovers.quarantine_leftovers(
                    "/Applications/Example.app", {"bundle_id": "com.example"}, leftovers, config
                )

            self.assertEqual(entries, [])
            self.assertTrue((source / "large.bin").exists())

    def test_quarantine_error_does_not_report_completed_app_trash_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".Trash").mkdir()
            app = home / "Applications/Demo.app"
            app.mkdir(parents=True)
            ledger = home / "deletions.jsonl"
            config = enabled_config(home)
            config["app_cleanup"].update(
                allow_unknown_restore_source=True,
                delete_mode="trash",
                deletion_ledger=str(ledger),
            )

            def fail_after_move(*_args):
                self.assertFalse(app.exists())
                raise OSError("ledger unavailable")

            with (
                patch.dict(os.environ, {"HOME": str(home)}),
                patch.object(maintenance_core, "get_restore_source", return_value={"source": "test"}),
                patch.object(maintenance_core, "app_metadata", return_value={"bundle_id": "com.example"}),
                patch.object(maintenance_core, "run_delete_hooks", return_value=True),
                patch.object(maintenance_core, "terminate_app_processes"),
                patch.object(app_leftovers, "discover_leftovers", return_value=[{"path": str(home / "config")}]),
                patch.object(app_leftovers, "quarantine_leftovers", side_effect=fail_after_move) as quarantine,
                patch.object(maintenance_core, "notify_user") as notify,
            ):
                succeeded = maintenance_core.delete_app(str(app), config)

            self.assertTrue(succeeded)
            self.assertFalse(app.exists())
            self.assertTrue((home / ".Trash/Demo.app").exists())
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(entry["action"], "trashed")
            quarantine.assert_called_once()
            self.assertTrue(any("config quarantine" in call.args[1].lower() for call in notify.call_args_list))


if __name__ == "__main__":
    unittest.main()
