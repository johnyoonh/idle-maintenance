import unittest
from unittest.mock import Mock, patch

import maintenance_interactive as maintenance


def process(pid, cpu, elapsed=60, comm="sample", etime="00:01:00"):
    return {
        "pid": pid,
        "user": "tester",
        "cpu": cpu,
        "etime": etime,
        "elapsed_seconds": elapsed,
        "comm": comm,
        "command": f"/usr/bin/{comm}",
    }


class MaintenanceInteractiveTests(unittest.TestCase):
    def test_queue_snooze_uses_configured_window(self):
        item = {"last_prompted": 1_000}

        self.assertTrue(maintenance.queue_item_is_snoozed(item, 24, now=1_000 + 86_399))
        self.assertFalse(maintenance.queue_item_is_snoozed(item, 24, now=1_000 + 86_400))

    def test_high_cpu_requires_all_samples(self):
        snapshots = iter([
            {11: process(11, 75)},
            {11: process(11, 70)},
            {11: process(11, 10)},
        ])
        sleeper = Mock()
        config = {
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
            "process_cpu_sample_count": 3,
            "process_cpu_sample_interval_seconds": 30,
        }

        result = maintenance.get_candidate_processes(
            config, snapshot_provider=lambda: next(snapshots), sleep_fn=sleeper
        )

        self.assertEqual(result, [])
        self.assertEqual(sleeper.call_count, 2)

    def test_sustained_high_cpu_includes_samples_and_reason(self):
        snapshots = iter([
            {11: process(11, 75)},
            {11: process(11, 70)},
            {11: process(11, 65)},
        ])
        config = {
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
            "process_cpu_sample_count": 3,
            "process_cpu_sample_interval_seconds": 30,
        }

        result = maintenance.get_candidate_processes(
            config, snapshot_provider=lambda: next(snapshots), sleep_fn=lambda _: None
        )

        self.assertEqual(result[0]["cpu_samples"], [75, 70, 65])
        self.assertIn("3 samples over 60s", result[0]["reason"])

    def test_long_running_process_does_not_need_high_cpu_samples(self):
        snapshot = {
            22: process(
                22,
                12,
                elapsed=25 * 3600,
                comm="worker",
                etime="1-01:00:00",
            )
        }
        config = {
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
            "process_cpu_sample_count": 3,
            "process_cpu_sample_interval_seconds": 30,
        }

        result = maintenance.get_candidate_processes(
            config, snapshot_provider=lambda: snapshot, sleep_fn=lambda _: None
        )

        self.assertEqual(len(result), 1)
        self.assertIn("Running 1-01:00:00", result[0]["reason"])

    @patch("maintenance_interactive.time.time", return_value=1_735_776_000)
    def test_app_detail_explains_age_threshold_and_source(self, _time):
        detail = maintenance.app_usage_detail("2025-01-01 (observed)", 90)

        self.assertIn("observed usage", detail)
        self.assertIn("stale threshold: 90 days", detail)


if __name__ == "__main__":
    unittest.main()
