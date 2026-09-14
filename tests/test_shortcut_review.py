import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import maint
from shortcut_review import normalize_command, render_result, run_shortcut_review


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

    def test_browser_audit_runs_before_keyboard_refresh_and_is_retained(self):
        calls = []
        audit_payload = {
            "ok": True,
            "has_conflicts": True,
            "conflicts": [{"message": "Simplify / _execute_action uses alt+shift+s"}],
        }

        def runner(command, **_kwargs):
            calls.append(command)
            stdout = json.dumps(audit_payload) if command[0] == "browser-audit" else "ok"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = run_shortcut_review(
            {
                "browser_shortcut_audit_command": ["browser-audit"],
                "return_flashcard_refresh_command": ["keyboard-refresh"],
                "return_shortcut_popup_command": ["keyboard-popup"],
            },
            runner=runner,
            provider="keyboard",
            state_path=Path(tempfile.mkdtemp()) / "state.json",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [["browser-audit"], ["keyboard-refresh"], ["keyboard-popup"]])
        self.assertEqual(result["browser_audit"], audit_payload)
        self.assertIn("Browser shortcut audit: 1 conflict(s)", render_result(result))

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

    def test_automatic_review_uses_six_hour_cooldown_and_weekday_limit(self):
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
            first = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 8, tzinfo=timezone.utc),
            )
            cooldown = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 13, 59, tzinfo=timezone.utc),
            )
            second = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 14, tzinfo=timezone.utc),
            )
            capped = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 20, tzinfo=timezone.utc),
            )
            self.assertTrue(first["ok"])
            self.assertEqual(cooldown["reason"], "cooldown")
            self.assertTrue(second["ok"])
            self.assertEqual(capped["reason"], "daily-limit")
            self.assertEqual(capped["dailyLimit"], 2)
            self.assertEqual(sum(command[0].endswith("popup") for command in calls), 2)

    def test_weekend_allows_three_automatic_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            config = {
                "return_flashcard_refresh_command": ["keyboard-refresh"],
                "return_shortcut_popup_command": ["keyboard-popup"],
            }
            local_zone = ZoneInfo("America/Chicago")
            results = [
                run_shortcut_review(
                    config,
                    automatic=True,
                    runner=runner,
                    state_path=state,
                    now=datetime(2026, 9, 12, hour, tzinfo=local_zone),
                )
                for hour in (1, 7, 13, 19)
            ]

            self.assertTrue(all(result["ok"] for result in results))
            self.assertEqual(results[-1]["reason"], "daily-limit")
            self.assertEqual(results[-1]["dailyLimit"], 3)
            self.assertEqual(len(calls), 6)

    def test_successful_manual_review_restarts_automatic_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            config = {
                "return_flashcard_refresh_command": ["keyboard-refresh"],
                "return_shortcut_popup_command": ["keyboard-popup"],
            }
            manual = run_shortcut_review(
                config,
                provider="keyboard",
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
            )
            automatic = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 13, tzinfo=timezone.utc),
            )

            self.assertTrue(manual["ok"])
            self.assertEqual(automatic["reason"], "cooldown")
            self.assertEqual(len(calls), 2)

    def test_failed_review_does_not_consume_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            fail = True

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    4 if fail else 0,
                    stdout="",
                    stderr="failed" if fail else "",
                )

            config = {
                "return_flashcard_refresh_command": ["keyboard-refresh"],
                "return_shortcut_popup_command": ["keyboard-popup"],
            }
            first = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
            )
            fail = False
            second = run_shortcut_review(
                config,
                automatic=True,
                runner=runner,
                state_path=state,
                now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
            )

            self.assertFalse(first["ok"])
            self.assertTrue(second["ok"])
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["reviewHistory"]), 1)

    def test_legacy_daily_marker_migrates_without_an_extra_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "lastAutomaticReviewDate": "2026-09-10",
                        "providers": {
                            "keyboard": {"lastShownAt": "2026-09-10T12:00:00+00:00"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = run_shortcut_review(
                {},
                automatic=True,
                runner=lambda *_args, **_kwargs: self.fail("provider should not run"),
                state_path=state,
                now=datetime(2026, 9, 10, 13, tzinfo=timezone.utc),
            )

            self.assertEqual(result["reason"], "cooldown")
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertNotIn("lastAutomaticReviewDate", saved)
            self.assertEqual(len(saved["reviewHistory"]), 1)

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
