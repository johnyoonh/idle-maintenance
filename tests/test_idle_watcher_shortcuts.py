import subprocess
import unittest
from unittest.mock import Mock, patch

import idle_watcher


class IdleWatcherShortcutTests(unittest.TestCase):
    def test_trigger_refreshes_shortcuts_after_handoff(self):
        commands = []

        def command_runner(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        shortcut_runner = Mock(return_value={"ok": True, "steps": []})
        config = {"handoff_url": "taskforge://upcoming"}
        with (
            patch.object(idle_watcher, "load_config", return_value=config),
            patch.object(idle_watcher, "get_handoff_url", return_value="taskforge://upcoming"),
        ):
            result = idle_watcher.trigger_maintenance(
                command_runner=command_runner,
                shortcut_runner=shortcut_runner,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(commands[0][-1].endswith("maintenance_interactive.py"))
        self.assertEqual(commands[1], ["open", "taskforge://upcoming"])
        shortcut_runner.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
