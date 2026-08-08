import subprocess
import unittest
from unittest.mock import patch

import idle_watcher


class IdleWatcherReviewOrderTests(unittest.TestCase):
    def test_shortcut_review_finishes_before_handoff(self):
        events = []

        def command_runner(command, **kwargs):
            events.append(("command", command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        def shortcut_runner(config):
            events.append(("shortcuts", config, {}))
            return {"ok": True, "steps": []}

        config = {"handoff_url": "taskforge://upcoming"}
        with (
            patch.object(idle_watcher, "load_config", return_value=config),
            patch.object(idle_watcher, "get_handoff_url", return_value="taskforge://upcoming"),
        ):
            idle_watcher.trigger_maintenance(
                command_runner=command_runner,
                shortcut_runner=shortcut_runner,
            )

        self.assertTrue(events[0][1][-1].endswith("maintenance_interactive.py"))
        self.assertEqual(events[0][2]["env"]["IDLE_MAINTENANCE_SKIP_SHORTCUT_REVIEW"], "1")
        self.assertEqual(events[1][0], "shortcuts")
        self.assertEqual(events[2][1], ["open", "taskforge://upcoming"])


if __name__ == "__main__":
    unittest.main()
