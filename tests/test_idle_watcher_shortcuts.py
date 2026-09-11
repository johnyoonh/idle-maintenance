import subprocess
import unittest
from unittest.mock import patch

import idle_watcher


class IdleWatcherShortcutTests(unittest.TestCase):
    def test_automatic_review_waits_for_quiet_input_window(self):
        state = {"was_idle": False, "review_pending": False}

        def sample(idle_time):
            state["was_idle"], state["review_pending"], trigger = (
                idle_watcher.review_gate_transition(
                    idle_time,
                    was_idle=state["was_idle"],
                    review_pending=state["review_pending"],
                    away_seconds=600,
                    active_cutoff_seconds=30,
                    review_idle_seconds=30,
                    review_idle_max_seconds=300,
                )
            )
            return trigger

        self.assertFalse(sample(601))
        self.assertFalse(sample(0))
        self.assertTrue(state["review_pending"])
        self.assertFalse(sample(10))
        self.assertFalse(sample(300))
        self.assertTrue(sample(30))
        self.assertFalse(state["review_pending"])

    def test_trigger_runs_resume_router_after_interactive_reviews(self):
        events = []

        def command_runner(command, **kwargs):
            events.append(("command", command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        config = {
            "return_focus_command": ["open", "hammerspoon://resumerouter"],
            "handoff_url": "taskforge://upcoming",
        }
        with (
            patch.object(idle_watcher, "load_config", return_value=config),
            patch.object(idle_watcher, "get_handoff_url", return_value="taskforge://upcoming"),
        ):
            result = idle_watcher.trigger_maintenance(
                command_runner=command_runner,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(events[0][1][-1].endswith("maintenance_interactive.py"))
        self.assertNotIn("IDLE_MAINTENANCE_SKIP_SHORTCUT_REVIEW", events[0][2]["env"])
        self.assertEqual(events[1][1], ["open", "hammerspoon://resumerouter"])
        self.assertFalse(result["fallback"])

    def test_trigger_opens_obsidian_and_current_page_srs_after_focus(self):
        events = []

        def command_runner(command, **kwargs):
            events.append(("command", command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        config = {
            "return_focus_command": ["open", "hammerspoon://resumerouter"],
            "return_obsidian_command": ["open", "-a", "Obsidian"],
            "return_obsidian_srs_command": [
                "open",
                "obsidian://adv-uri?vault=wiki&commandid=obsidian-spaced-repetition%3Asrs-review-flashcards-in-note",
            ],
            "return_obsidian_srs_delay_seconds": 0,
        }
        with patch.object(idle_watcher, "load_config", return_value=config):
            result = idle_watcher.trigger_maintenance(
                command_runner=command_runner,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(events[0][1][-1].endswith("maintenance_interactive.py"))
        self.assertEqual(events[1][1], ["open", "hammerspoon://resumerouter"])
        self.assertEqual(events[2][1], ["open", "-a", "Obsidian"])
        self.assertEqual(
            events[3][1],
            [
                "open",
                "obsidian://adv-uri?vault=wiki&commandid=obsidian-spaced-repetition%3Asrs-review-flashcards-in-note",
            ],
        )
        self.assertTrue(result["page_review"]["ok"])
        self.assertEqual(len(result["page_review"]["steps"]), 2)

    def test_failed_resume_router_uses_legacy_handoff(self):
        events = []

        def command_runner(command, **kwargs):
            events.append((command, kwargs))
            returncode = 9 if command == ["open", "hammerspoon://resumerouter"] else 0
            return subprocess.CompletedProcess(command, returncode)

        config = {
            "return_focus_command": ["open", "hammerspoon://resumerouter"],
            "handoff_url": "taskforge://upcoming",
        }
        with (
            patch.object(idle_watcher, "load_config", return_value=config),
            patch.object(idle_watcher, "get_handoff_url", return_value="taskforge://upcoming"),
        ):
            result = idle_watcher.trigger_maintenance(command_runner=command_runner)

        self.assertFalse(result["ok"])
        self.assertTrue(result["fallback"])
        self.assertEqual(events[-1][0], ["open", "taskforge://upcoming"])


if __name__ == "__main__":
    unittest.main()
