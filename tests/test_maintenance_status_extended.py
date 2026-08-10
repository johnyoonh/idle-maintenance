import subprocess
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

        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

        with patch.object(extended.base, "collect_status", return_value=base_status):
            status = extended.collect_status(
                self.config(), command_runner=runner, home=Path("/tmp/home"), now=1000
            )
        self.assertFalse(status["away_return_review"]["running"])
        self.assertTrue(extended._healthy(status))
        self.assertIn("Away-return review (optional)", status["text"])
        self.assertIn("Hammerspoon context router configured", status["text"])


if __name__ == "__main__":
    unittest.main()
