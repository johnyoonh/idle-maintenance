import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import maint


class MaintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.cache = self.root / "cache.json"
        self.sessions = self.root / "session.json"
        self.paths = patch.multiple(
            maint,
            STATE_PATH=str(self.state),
            CACHE_PATH=str(self.cache),
            SESSION_PATH=str(self.sessions),
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.addCleanup(self.temp.cleanup)
        self.suggestion = {
            "script": "sample",
            "command": "sample --clean",
            "description": "Clean a synthetic cache",
            "frequency_hours": 24,
        }
        self.cache.write_text(json.dumps({"scripts": [self.suggestion]}))

    def read_state(self):
        return json.loads(self.state.read_text())

    @patch("maint.show_next_suggestion")
    def test_dismiss_is_state_only_and_reversible(self, _show_next):
        self.assertEqual(maint.main(["dismiss", "sample"]), 0)
        self.assertIn("sample", self.read_state()["disabled"])

        self.assertEqual(maint.main(["enable", "sample"]), 0)
        self.assertNotIn("sample", self.read_state()["disabled"])

    @patch("maint.show_next_suggestion")
    def test_later_uses_frequency_without_disabling(self, _show_next):
        self.assertEqual(maint.main(["later", "sample"]), 0)

        state = self.read_state()
        self.assertIn("sample", state["dismissed"])
        self.assertNotIn("sample", state["disabled"])

    @patch("maint.show_next_suggestion")
    def test_legacy_delete_form_maps_to_safe_dismiss(self, _show_next):
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = maint.main(["sample", "2"])

        self.assertEqual(result, 0)
        self.assertIn("Deprecated", stderr.getvalue())
        self.assertIn("sample", self.read_state()["disabled"])

    def test_run_records_success(self):
        runner = Mock()
        runner.return_value.returncode = 0

        result = maint.run_command(self.suggestion, runner=runner)

        self.assertEqual(result, 0)
        self.assertIn("sample", self.read_state()["last_run"])
        runner.assert_called_once_with("sample --clean", shell=True, check=False)

    def test_canonical_parser_prefers_action_first(self):
        self.assertEqual(
            maint.parse_args(["preview", "sample"]),
            ("preview", "sample", False, False),
        )


if __name__ == "__main__":
    unittest.main()
