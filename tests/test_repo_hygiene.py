from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def check_ignored(self, relative_path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "check-ignore", "--no-index", "--verbose", relative_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_private_runtime_and_generated_paths_are_ignored_by_tracked_rules(self):
        paths = (
            ".beads/issues.jsonl",
            ".claude/settings.local.json",
            ".env",
            ".local-fixtures/private.json",
            ".private/incident.json",
            "AGENTS.local.md",
            "IdleMaintenance.app/Contents/MacOS/IdleMaintenance",
            "IdleMaintenance.log",
            "activity-intelligence.sqlite3-wal",
            "config.json",
            "credentials.json",
            "resource-monitor-history.jsonl",
            "resource-monitor-state.json",
            "tmp-private/sample-note.md",
        )

        for path in paths:
            with self.subTest(path=path):
                result = self.check_ignored(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith(".gitignore:"), result.stdout)

    def test_public_source_and_synthetic_fixtures_remain_trackable(self):
        paths = (
            "README.md",
            "prompt-suggestions.json",
            "tests/fixtures/sample.jsonl",
        )

        for path in paths:
            with self.subTest(path=path):
                result = self.check_ignored(path)
                self.assertEqual(result.returncode, 1, result.stdout)

    def test_runtime_state_files_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "--", "custom_whitelist.json", "stale_queue.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
