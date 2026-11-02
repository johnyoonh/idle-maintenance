import unittest
from unittest.mock import MagicMock, patch

import auto_restart


class AutoRestartTests(unittest.TestCase):
    def test_uptime_calculation(self):
        fake_runner = MagicMock()
        fake_runner.return_value = MagicMock(
            returncode=0,
            stdout="{ sec = 1000, usec = 0 } Mon Oct 12 00:00:00 2026\n",
        )
        uptime = auto_restart.get_uptime_seconds(runner=fake_runner, now_fn=lambda: 2000.0)
        self.assertEqual(uptime, 1000.0)

    def test_evaluate_eligible_when_criteria_met(self):
        config = {
            "auto_restart": {
                "enabled": True,
                "uptime_days": 14,
                "sleep_hours": 18,
                "idle_hours": 18,
                "require_ac": True,
            }
        }
        state = {
            "last_heartbeat": 1_000_000.0,
        }
        now = 1_000_000.0 + (19 * 3600)  # 19 hours later

        with (
            patch("auto_restart.get_uptime_seconds", return_value=15 * 86400),
            patch("auto_restart.is_ac_power", return_value=True),
            patch("auto_restart.is_backup_active", return_value=False),
        ):
            res = auto_restart.evaluate_restart_eligibility(
                config=config,
                state=state,
                now=now,
                idle_reader=lambda: 19 * 3600,
            )

        self.assertTrue(res["eligible"])
        self.assertEqual(res["reasons"], [])

    def test_evaluate_ineligible_when_active_or_low_uptime(self):
        config = {
            "auto_restart_enabled": True,
            "auto_restart_uptime_days": 14,
            "auto_restart_sleep_hours": 18,
            "auto_restart_idle_hours": 18,
            "auto_restart_require_ac": True,
        }
        state = {
            "last_heartbeat": 1_000_000.0,
        }
        now = 1_000_000.0 + 300  # only 5 minutes later

        with (
            patch("auto_restart.get_uptime_seconds", return_value=10 * 86400),  # only 10 days
            patch("auto_restart.is_ac_power", return_value=True),
            patch("auto_restart.is_backup_active", return_value=False),
        ):
            res = auto_restart.evaluate_restart_eligibility(
                config=config,
                state=state,
                now=now,
                idle_reader=lambda: 10,
            )

        self.assertFalse(res["eligible"])
        self.assertTrue(any("uptime" in r for r in res["reasons"]))
        self.assertTrue(any("sleep gap" in r for r in res["reasons"]))

    def test_evaluate_ineligible_on_battery(self):
        config = {
            "auto_restart_enabled": True,
            "auto_restart_uptime_days": 14,
            "auto_restart_sleep_hours": 18,
            "auto_restart_idle_hours": 18,
            "auto_restart_require_ac": True,
        }
        state = {"last_heartbeat": 1_000_000.0}
        now = 1_000_000.0 + (20 * 3600)

        with (
            patch("auto_restart.get_uptime_seconds", return_value=20 * 86400),
            patch("auto_restart.is_ac_power", return_value=False),
            patch("auto_restart.is_backup_active", return_value=False),
        ):
            res = auto_restart.evaluate_restart_eligibility(
                config=config,
                state=state,
                now=now,
                idle_reader=lambda: 20 * 3600,
            )

        self.assertFalse(res["eligible"])
        self.assertIn("system is on battery power", res["reasons"])

    def test_evaluate_ineligible_when_backup_running(self):
        config = {
            "auto_restart_enabled": True,
            "auto_restart_uptime_days": 14,
            "auto_restart_sleep_hours": 18,
            "auto_restart_idle_hours": 18,
            "auto_restart_require_ac": True,
        }
        state = {"last_heartbeat": 1_000_000.0}
        now = 1_000_000.0 + (20 * 3600)

        with (
            patch("auto_restart.get_uptime_seconds", return_value=20 * 86400),
            patch("auto_restart.is_ac_power", return_value=True),
            patch("auto_restart.is_backup_active", return_value=True),
        ):
            res = auto_restart.evaluate_restart_eligibility(
                config=config,
                state=state,
                now=now,
                idle_reader=lambda: 20 * 3600,
            )

        self.assertFalse(res["eligible"])
        self.assertIn("Time Machine backup is actively running", res["reasons"])

    def test_dry_run_does_not_call_system_restart(self):
        fake_runner = MagicMock()
        success, msg = auto_restart.execute_restart(dry_run=True, runner=fake_runner)
        self.assertTrue(success)
        self.assertIn("dry-run", msg)
        fake_runner.assert_not_called()

    def test_prefers_authrestart_when_filevault_active(self):
        fake_runner = MagicMock()
        fake_runner.side_effect = [
            MagicMock(returncode=0, stdout="true\n"),  # isactive
            MagicMock(returncode=0, stdout=""),        # authrestart
        ]
        success, msg = auto_restart.execute_restart(
            dry_run=False, prefer_authrestart=True, runner=fake_runner
        )
        self.assertTrue(success)
        self.assertIn("authrestart", msg)


if __name__ == "__main__":
    unittest.main()
