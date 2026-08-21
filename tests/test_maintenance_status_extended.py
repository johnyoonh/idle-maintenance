import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance_status_extended as extended


class MaintenanceStatusExtendedTests(unittest.TestCase):
    def config(self):
        return {
            "idle_threshold_minutes": 10,
            "post_trigger_cooldown_seconds": 3600,
            "return_focus_command": ["open", "hammerspoon://resumerouter"],
        }

    def test_away_return_status_reports_running_policy(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="123\n", stderr="")

        status = extended.away_return_review_status(
            self.config(), command_runner=runner, home=Path("/tmp/home")
        )
        self.assertTrue(status["running"])
        self.assertEqual(status["idle_threshold_seconds"], 600)
        self.assertEqual(status["return_idle_below_seconds"], 30)
        self.assertEqual(status["cooldown_seconds"], 3600)
        self.assertTrue(status["resume_focus_configured"])

    def test_optional_watcher_does_not_change_core_health(self):
        base_status = {
            "runner": {"healthy": True},
            "resource_monitor": {"state": "healthy", "healthy": True},
            "text": "Idle maintenance status",
        }
        actions = {
            "queued": 2,
            "running": 1,
            "failed": 1,
            "most_recent_completion": {
                "app_path": "/Applications/Done.app",
                "state": "completed",
                "finished_at": 900,
                "result": {"outcome": "trashed"},
            },
            "most_recent_failure": {
                "app_path": "/Applications/Failed.app",
                "state": "failed",
                "finished_at": 950,
                "error": "permission denied",
                "result": {"outcome": "delete-failed"},
            },
        }

        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extended.base, "collect_status", return_value=base_status
        ), patch.object(extended, "app_action_status", return_value=actions):
            status = extended.collect_status(
                self.config(), command_runner=runner, home=Path(tmp), now=1000
            )
        self.assertFalse(status["away_return_review"]["running"])
        self.assertTrue(extended._healthy(status))
        self.assertEqual(status["app_actions"]["queued"], 2)
        self.assertEqual(status["app_actions"]["running"], 1)
        self.assertEqual(status["app_actions"]["failed"], 1)
        self.assertIn("App actions:", status["text"])
        self.assertIn("2 queued • 1 running • 1 failed", status["text"])
        self.assertIn("Done.app — trashed", status["text"])
        self.assertIn("Away-return review (optional)", status["text"])
        self.assertIn("Hammerspoon context router configured", status["text"])

    def test_collect_status_uses_home_scoped_app_action_state(self):
        base_status = {
            "runner": {"healthy": True},
            "resource_monitor": {"state": "not-started", "healthy": True},
            "text": "Idle maintenance status",
        }

        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extended.base, "collect_status", return_value=base_status
        ), patch.object(extended, "app_action_status", return_value={
            "queued": 0,
            "running": 0,
            "failed": 0,
            "most_recent_completion": None,
            "most_recent_failure": None,
        }) as action_status:
            extended.collect_status(self.config(), command_runner=runner, home=Path(tmp), now=1000)
        expected = Path(tmp) / "Library" / "Application Support" / "idle-maintenance" / "app-actions.json"
        action_status.assert_called_once_with(state_path=str(expected), now=1000.0)


if __name__ == "__main__":
    unittest.main()
