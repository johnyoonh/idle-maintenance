from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import maintenance_core
from process_review import handle_process_action, prompt_process


class TerminalInvestigationTests(unittest.TestCase):
    def test_actionable_notification_opens_log_with_default_file_handler(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        history = Path("/tmp/synthetic resource history.jsonl")

        with (
            patch("maintenance_core._terminal_notifier_path", return_value="/test/terminal-notifier"),
            patch("maintenance_core.subprocess.run", return_value=completed) as run,
        ):
            maintenance_core.notify_user(
                "Idle Maintenance resource incident",
                "sample sustained 25 MiB/s",
                click_path=history,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/test/terminal-notifier")
        self.assertEqual(command[1:5], ["-title", "Idle Maintenance resource incident", "-message", "sample sustained 25 MiB/s"])
        self.assertEqual(command[5], "-execute")
        resolved = str(history.resolve())
        self.assertEqual(
            command[6],
            f"/usr/bin/open -a 'Visual Studio Code' -- {shlex.quote(resolved)}"
            f" || /usr/bin/open -t -- {shlex.quote(resolved)}",
        )
        self.assertNotIn("osascript", command)

    def test_actionable_notification_falls_back_to_applescript(self):
        with (
            patch("maintenance_core._terminal_notifier_path", return_value=None),
            patch("maintenance_core.subprocess.run") as run,
        ):
            maintenance_core.notify_user(
                "Idle Maintenance resource incident",
                "sample sustained 25 MiB/s",
                click_path="/tmp/resource-monitor-history.jsonl",
            )

        self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_launch_file_is_private_self_deleting_and_shell_quoted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(
                maintenance_core.create_codex_launch_file(
                    "synthetic prompt with 'quotes'",
                    directory,
                    directory,
                )
            )
            content = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o700)
        self.assertIn('rm -f -- "$0"', content)
        self.assertIn("exec /bin/zsh -lic", content)
        self.assertIn("codex", content)
        self.assertIn(str(Path(directory)), content)

    def test_iterm_launch_uses_launchservices_not_apple_events(self):
        commands = []

        def launch(command, **_kwargs):
            commands.append(command)
            os.unlink(command[-1])  # Simulate the command file starting in iTerm.
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            result = maintenance_core.open_codex_in_terminal(
                "synthetic prompt",
                directory,
                launch_runner=launch,
                clipboard_fn=lambda _text: True,
                launch_directory=directory,
            )

        self.assertEqual(result, (True, "iTerm", True))
        self.assertEqual(commands[0][:3], ["/usr/bin/open", "-b", "com.googlecode.iterm2"])
        self.assertNotIn("osascript", commands[0])

    def test_failed_launch_tries_terminal_and_removes_command_file(self):
        commands = []

        def launch(command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=1, stdout="", stderr="not installed")

        with tempfile.TemporaryDirectory() as directory:
            result = maintenance_core.open_codex_in_terminal(
                "synthetic prompt",
                directory,
                launch_runner=launch,
                clipboard_fn=lambda _text: True,
                launch_directory=directory,
            )
            leftovers = list(Path(directory).glob("*.command"))

        self.assertEqual(result, (False, None, True))
        self.assertEqual([command[2] for command in commands], ["com.googlecode.iterm2", "com.apple.Terminal"])
        self.assertEqual(leftovers, [])

    def test_process_action_notifies_when_tab_cannot_open(self):
        notifications = []
        core = SimpleNamespace(
            open_codex_in_terminal=Mock(return_value=(False, None, True)),
            process_cwd=lambda _proc: "/",
            notify_user=lambda title, message: notifications.append((title, message)),
            log=Mock(),
        )
        process = {"pid": 42, "comm": "/usr/bin/sample", "command": "sample"}

        outcome = handle_process_action(core, process, "INVESTIGATE", {})

        self.assertEqual(outcome, "failed")
        self.assertIn("copied to the clipboard", notifications[0][1])

    def test_one_shot_monitor_prompt_includes_copy_text_and_uses_shared_helper(self):
        process = {
            "pid": 42,
            "comm": "/usr/bin/sample",
            "command": "/usr/bin/sample --work",
            "cpu": 1.0,
            "cpu_samples": [1.0],
            "etime": "00:10:00",
            "reason": "synthetic resource review",
        }
        core = SimpleNamespace(BASE_DIR="/repo")

        with patch("process_review.legacy_prompt", return_value="KEEP") as helper:
            outcome = prompt_process(core, process)

        self.assertEqual(outcome, "KEEP")
        payload = helper.call_args.args[1]
        self.assertEqual(payload["mode"], "process")
        self.assertIn("Investigate this high-impact macOS process", payload["copyText"])
        self.assertIn("CPU samples: 1.0%", payload["headline"])


if __name__ == "__main__":
    unittest.main()
