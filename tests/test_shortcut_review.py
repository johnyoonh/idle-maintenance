import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
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
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "refresh")
        self.assertEqual(calls, [["kb", "export-srs"]])

    def test_maint_shortcuts_json_uses_canonical_workflow(self):
        result = {"ok": True, "failed_step": None, "error": "", "steps": []}
        output = io.StringIO()
        with patch("shortcut_review.run_shortcut_review", return_value=result), redirect_stdout(output):
            code = maint.main(["shortcuts", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
