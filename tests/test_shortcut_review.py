import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import maint
from shortcut_review import normalize_command, run_shortcut_review


class ShortcutReviewTests(unittest.TestCase):
    def test_normalize_command_expands_home_without_shell(self):
        command = normalize_command("~/bin/kb popup --force", Path("/tmp/home"))
        self.assertEqual(command, ["/tmp/home/bin/kb", "popup", "--force"])

    def test_refresh_succeeds_before_popup(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        result = run_shortcut_review(
            {
                "return_flashcard_refresh_command": ["kb", "export-srs", "--mode", "focused"],
                "return_shortcut_popup_command": ["kb", "popup", "--force"],
            },
            runner=runner,
            provider="keyboard",
            state_path=Path(tempfile.mkdtemp()) / "state.json",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [
            ["kb", "export-srs", "--mode", "focused"],
            ["kb", "popup", "--force"],
        ])

    def test_failed_refresh_prevents_stale_popup(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 4, stdout="", stderr="refresh failed")

        result = run_shortcut_review(
            {
                "return_flashcard_refresh_command": ["kb", "export-srs"],
                "return_shortcut_popup_command": ["kb", "popup"],
            },
            runner=runner,
            provider="keyboard",
            state_path=Path(tempfile.mkdtemp()) / "state.json",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "refresh")
        self.assertEqual(calls, [["kb", "export-srs"]])

    def test_maint_shortcuts_json_uses_canonical_workflow(self):
        result = {"ok": True, "failed_step": None, "error": "", "steps": []}
        output = io.StringIO()
        with patch("shortcut_review.run_shortcut_review", return_value=result) as review, redirect_stdout(output):
            code = maint.main(["shortcuts", "--provider", "apple", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        review.assert_called_once_with(provider="apple")

    def test_next_due_rotates_by_success_time(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({
                "providers": {
                    "keyboard": {"lastShownAt": "2026-09-09T10:00:00+00:00"},
                    "apple": {"lastShownAt": "2026-09-01T10:00:00+00:00"},
                }
            }))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                stdout = json.dumps({"actionableCount": 2}) if command[0] == "apple-refresh" else ""
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            result = run_shortcut_review(
                {
                    "return_flashcard_refresh_command": ["keyboard-refresh"],
                    "return_shortcut_popup_command": ["keyboard-popup"],
                    "apple_shortcut_review_refresh_command": ["apple-refresh"],
                    "apple_shortcut_review_popup_command": ["apple-popup"],
                },
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["provider"], "apple")
            self.assertEqual(calls, [["apple-refresh"], ["apple-popup"]])

    def test_automatic_review_runs_once_per_local_day(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            config = {
                "return_flashcard_refresh_command": ["keyboard-refresh"],
                "return_shortcut_popup_command": ["keyboard-popup"],
                "apple_shortcut_review_refresh_command": ["apple-refresh"],
                "apple_shortcut_review_popup_command": ["apple-popup"],
            }
            now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
            first = run_shortcut_review(config, automatic=True, runner=runner, state_path=state, now=now)
            second = run_shortcut_review(config, automatic=True, runner=runner, state_path=state, now=now)
            self.assertTrue(first["ok"])
            self.assertEqual(second["reason"], "already-reviewed-today")
            self.assertEqual(calls, [["keyboard-refresh"], ["keyboard-popup"]])

    def test_empty_apple_provider_falls_back_without_opening_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"providers": {"keyboard": {"lastShownAt": "2026-09-09T00:00:00+00:00"}}}))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                stdout = json.dumps({"actionableCount": 0}) if command[0] == "apple-refresh" else ""
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            result = run_shortcut_review(
                {
                    "return_flashcard_refresh_command": ["keyboard-refresh"],
                    "return_shortcut_popup_command": ["keyboard-popup"],
                    "apple_shortcut_review_refresh_command": ["apple-refresh"],
                    "apple_shortcut_review_popup_command": ["apple-popup"],
                },
                runner=runner,
                state_path=state,
            )
            self.assertEqual(result["provider"], "keyboard")
            self.assertEqual(calls, [["apple-refresh"], ["keyboard-refresh"], ["keyboard-popup"]])

    def test_parser_rejects_unknown_provider(self):
        self.assertEqual(maint.parse_shortcut_provider(["shortcuts"]), None)
        with self.assertRaisesRegex(ValueError, "keyboard or apple"):
            maint.parse_shortcut_provider(["shortcuts", "--provider", "voice"])


if __name__ == "__main__":
    unittest.main()
