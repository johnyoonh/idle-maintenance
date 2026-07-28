import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import maintenance_interactive as maintenance


def process(
    pid,
    cpu,
    *,
    elapsed=60,
    comm="/usr/bin/sample",
    command=None,
    etime="00:01:00",
    read=0,
    write=0,
    start=100,
):
    command = command or comm
    value = {
        "pid": pid,
        "ppid": 1,
        "uid": os.getuid(),
        "cpu": cpu,
        "etime": etime,
        "elapsed_seconds": elapsed,
        "start_time": "Sat Jul 25 12:00:00 2026",
        "start_abstime": start,
        "comm": comm,
        "command": command,
        "fingerprint": maintenance.command_fingerprint(comm, command),
        "io_read_bytes": read,
        "io_write_bytes": write,
    }
    value["process_key"] = maintenance.process_key(value)
    return value


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
            22: process(22, 12, elapsed=25 * 3600, comm="/usr/bin/worker", etime="1-01:00:00")
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

    def test_sustained_io_detects_low_cpu_process_with_attribution_boundary(self):
        mib = 1024 * 1024
        snapshots = iter([
            {31: process(31, 2, read=0, write=0)},
            {31: process(31, 2, read=150 * mib, write=100 * mib)},
            {31: process(31, 2, read=300 * mib, write=200 * mib)},
        ])
        config = {
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
            "process_cpu_sample_count": 1,
            "process_cpu_sample_interval_seconds": 30,
            "process_io_enabled": True,
            "process_io_sample_count": 3,
            "process_io_sample_interval_seconds": 10,
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_io_required_intervals": 2,
            "process_io_minimum_window_mib": 256,
        }
        result = maintenance.get_candidate_processes(
            config, snapshot_provider=lambda: next(snapshots), sleep_fn=lambda _: None
        )
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["average_total_mib_s"], 25.0)
        self.assertIn("I/O charged to the process averaged 25.0 MiB/s", result[0]["reason"])
        self.assertIn("not definitive physical-disk attribution", result[0]["reason"])

    def test_io_counter_reset_is_not_reported(self):
        mib = 1024 * 1024
        snapshots = iter([
            {31: process(31, 2, read=300 * mib, write=100 * mib)},
            {31: process(31, 2, read=10 * mib, write=5 * mib)},
        ])
        config = {
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
            "process_cpu_sample_count": 1,
            "process_cpu_sample_interval_seconds": 30,
            "process_io_enabled": True,
            "process_io_sample_count": 2,
            "process_io_sample_interval_seconds": 10,
            "process_high_io_total_mib_per_second": 1,
            "process_high_io_write_mib_per_second": 1,
            "process_io_required_intervals": 1,
            "process_io_minimum_window_mib": 1,
        }
        self.assertEqual(
            maintenance.get_candidate_processes(
                config, snapshot_provider=lambda: next(snapshots), sleep_fn=lambda _: None
            ),
            [],
        )

    def test_process_key_distinguishes_same_executable_arguments(self):
        left = process(1, 5, comm="/usr/bin/python3", command="python3 worker_a.py")
        right = process(2, 5, comm="/usr/bin/python3", command="python3 worker_b.py")
        self.assertNotEqual(maintenance.process_key(left), maintenance.process_key(right))

    def test_termination_refuses_reused_pid(self):
        expected = process(42, 80, command="python3 old.py", start=100)
        current = process(42, 80, command="python3 new.py", start=200)
        signaler = Mock()
        result = maintenance.terminate_process(
            expected,
            {"process_terminate_grace_seconds": 0},
            identity_provider=lambda _: current,
            signal_fn=signaler,
        )
        self.assertEqual(result, "stale")
        signaler.assert_not_called()

    def test_termination_uses_sigterm_only(self):
        expected = process(42, 80)
        identities = iter([expected, expected, expected])
        signaler = Mock()
        monotonic = iter([0.0, 0.0, 1.0, 1.0])
        result = maintenance.terminate_process(
            expected,
            {
                "process_terminate_grace_seconds": 1,
                "process_terminate_poll_seconds": 1,
            },
            identity_provider=lambda _: next(identities),
            signal_fn=signaler,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: next(monotonic),
        )
        self.assertEqual(result, "still_running")
        signaler.assert_called_once_with(42, signal.SIGTERM)
        self.assertEqual(maintenance.force_kill_process(expected), "unsupported")

    def test_investigation_prompt_is_rootless_and_non_attributive(self):
        proc = process(55, 2)
        proc.update({"reason": "high I/O"})
        prompt = maintenance.build_process_investigation_prompt(proc, {})
        self.assertNotIn("fs_usage", prompt)
        self.assertNotIn("sudo", prompt)
        self.assertIn("rootless", prompt)
        self.assertIn("not definitive physical-disk attribution", prompt)

    def test_protected_daemon_policy_and_main_app_quit_policy(self):
        daemon = process(1, 5, comm="mediaanalysisd", command="mediaanalysisd")
        mail = process(
            2,
            5,
            comm="/System/Applications/Mail.app/Contents/MacOS/Mail",
            command="/System/Applications/Mail.app/Contents/MacOS/Mail",
        )
        shortcuts = process(
            3,
            5,
            comm="/System/Applications/Shortcuts.app/Contents/MacOS/Shortcuts",
            command="/System/Applications/Shortcuts.app/Contents/MacOS/Shortcuts",
        )
        self.assertEqual(maintenance.process_action_policy(daemon), "protected")
        self.assertEqual(maintenance.process_action_policy(mail), "graceful-quit")
        self.assertEqual(maintenance.process_action_policy(shortcuts), "graceful-quit")

    @patch("maintenance_interactive.time.time", return_value=1_735_776_000)
    def test_app_detail_explains_age_threshold_and_source(self, _time):
        detail = maintenance.app_usage_detail("2025-01-01 (observed)", 90)
        self.assertIn("observed usage", detail)
        self.assertIn("stale threshold: 90 days", detail)

    def test_save_json_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertTrue(maintenance.save_json(path, {"ok": True}))
            self.assertEqual(maintenance.load_json(path, {}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
