import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_leftovers import discover_leftovers, quarantine_leftovers, summarize_leftovers


def base_config(tmp_path):
    return {
        "app_cleanup": {
            "leftover_review": {
                "enabled": True,
                "mode": "conservative",
                "action": "quarantine",
                "max_item_size_mb": 1,
                "max_total_size_mb": 2,
                "quarantine_dir": str(tmp_path / "quarantine"),
                "ledger": str(tmp_path / "ledger.jsonl"),
            }
        }
    }


class AppLeftoversTest(unittest.TestCase):
    def test_discovers_conservative_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library/Application Support/Example").mkdir(parents=True)
            (home / "Library/Application Support/Example/config.json").write_text("{}")
            (home / "Library/Preferences").mkdir(parents=True)
            (home / "Library/Preferences/com.example.app.plist").write_text("plist")
            (home / ".config/example").mkdir(parents=True)
            (home / ".config/example/settings.toml").write_text("setting = true")
            (home / "Library/Application Support/Example Cache").mkdir(parents=True)

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = discover_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    base_config(home),
                )

            paths = {Path(item["path"]).relative_to(home.resolve()).as_posix() for item in leftovers}
            self.assertIn("Library/Application Support/Example", paths)
            self.assertIn("Library/Preferences/com.example.app.plist", paths)
            self.assertIn(".config/example", paths)
            self.assertNotIn("Library/Application Support/Example Cache", paths)
            self.assertIn("Leftovers: 3 config items", summarize_leftovers(leftovers))

    def test_excludes_large_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / "Library/Application Support/Example"
            config_dir.mkdir(parents=True)
            (config_dir / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = discover_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    base_config(home),
                )

            self.assertEqual(len(leftovers), 1)
            self.assertFalse(leftovers[0]["eligible"])
            self.assertEqual(leftovers[0]["excluded_reason"], "item-size-limit")

    def test_quarantine_preserves_relative_path_and_writes_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = base_config(home)
            config_dir = home / "Library/Application Support/Example"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}")

            with patch.dict(os.environ, {"HOME": str(home)}):
                leftovers = discover_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    config,
                )
                entries = quarantine_leftovers(
                    "/Applications/Example.app",
                    {"bundle_id": "com.example.app"},
                    leftovers,
                    config,
                )

            self.assertFalse(config_dir.exists())
            self.assertEqual(len(entries), 1)
            quarantine_path = Path(entries[0]["quarantine_path"])
            self.assertTrue(quarantine_path.exists())
            self.assertTrue(str(quarantine_path).endswith("Library/Application Support/Example"))

            ledger_path = home / "ledger.jsonl"
            ledger_entry = json.loads(ledger_path.read_text().splitlines()[0])
            self.assertEqual(ledger_entry["original_path"], str(config_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
