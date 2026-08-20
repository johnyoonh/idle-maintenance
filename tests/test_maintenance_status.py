import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import maintenance_status


class MaintenanceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.support = self.home / "Library/Application Support/idle-maintenance"
        self.logs = self.home / "Library/Logs/wiki-automation"
        self.support.mkdir(parents=True)
        self.logs.mkdir(parents=True)
        self.now = 1_700_000_000
        self.config = {
            "scheduled_runner_status_command": ["~/bin/runner", "--status"],
            "app_snooze_hours": 720,
            "process_snooze_hours": 24,
            "keep_days_limit": 30,
            "keep_backoff_multiplier": 2,
            "keep_backoff_max_days": 365,
            "process_keep_days_limit": 1,
            "process_keep_backoff_multiplier": 2,
            "process_keep_backoff_max_days": 30,
            "terminal_suggestion_start_hour": 9,
            "terminal_suggestion_end_hour": 21,
        }

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, command, **_kwargs):
        if command[0] == "launchctl":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="state = not running\nlast exit code = 0\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Idle maintenance status\n- cleanup: due in 2h\n",
            stderr="",
        )

    def test_periodic_loaded_job_is_healthy_while_not_running(self):
        status = maintenance_status.collect_status(
            config=self.config,
            command_runner=self.runner,
            home=self.home,
            now=self.now,
        )

        self.assertTrue(status["runner"]["healthy"])
        self.assertIn("idle between scheduled runs", status["runner"]["summary"])
        self.assertIn("cleanup: due in 2h", status["text"])

    def test_status_summarizes_queues_and_backoff(self):
        (self.support / "stale_queue.json").write_text(json.dumps([
            {"path": "Applications/Sample.app", "last_prompted": self.now - 3600}
        ]))
        (self.support / "process_queue.json").write_text(json.dumps([
            {"comm": "sample", "last_prompted": 0}
        ]))
        (self.support / "custom_whitelist.json").write_text(json.dumps({
            "Applications/Kept.app": {"kept_at": self.now - 60, "keep_count": 1}
        }))
        (self.support / "state.json").write_text(json.dumps({
            "disabled": {"old-command": self.now}
        }))

        status = maintenance_status.collect_status(
            config=self.config,
            command_runner=self.runner,
            home=self.home,
            now=self.now,
        )

        self.assertEqual(status["queues"]["apps"]["queued"], 1)
        self.assertEqual(status["queues"]["apps"]["snoozed"], 1)
        self.assertEqual(status["queues"]["apps"]["backed_off"], 1)
        self.assertEqual(status["queues"]["terminal_disabled"], 1)
        self.assertEqual(status["pattern_intelligence"]["health"]["vector_backend"], "not-started")
        self.assertIn("Pattern intelligence:", status["text"])

    def test_latest_log_event_uses_last_nonempty_line(self):
        log = self.logs / "idle-maintenance-runtime.log"
        log.write_text("first\n\nlatest synthetic event\n")

        status = maintenance_status.collect_status(
            config=self.config,
            command_runner=self.runner,
            home=self.home,
            now=self.now,
        )

        self.assertEqual(status["latest_event"], "latest synthetic event")

    def test_missing_runner_degrades_to_launchd_status(self):
        config = dict(self.config, scheduled_runner_status_command=[])

        status = maintenance_status.collect_status(
            config=config,
            command_runner=self.runner,
            home=self.home,
            now=self.now,
        )

        self.assertFalse(status["runner"]["status_command_available"])
        self.assertIn("No runner command", status["runner"]["status_error"])


if __name__ == "__main__":
    unittest.main()
