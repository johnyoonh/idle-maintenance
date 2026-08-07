import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import process_identity as identity
from process_review import (
    investigation_prompt,
    known_process_guidance,
    process_action_policy,
    should_suppress_process_alert,
)
from process_sampling import get_candidate_processes
from process_triage import triage_process
from resource_monitor import MIB, ResourceMonitor, run_monitor


def proc(
    pid=42,
    *,
    cpu=1.0,
    read=0,
    write=0,
    command="sample",
    comm="sample",
    start=100,
):
    value = {
        "pid": pid,
        "ppid": 1,
        "uid": os.getuid(),
        "cpu": cpu,
        "etime": "1-01:00:00",
        "elapsed_seconds": 25 * 3600,
        "start_time": "Fri Aug 7 12:00:00 2026",
        "start_abstime": start,
        "comm": comm,
        "command": command,
        "fingerprint": identity.fingerprint(command),
        "io_read_bytes": read,
        "io_write_bytes": write,
    }
    value["process_key"] = identity.key(value)
    return value


class SmartProcessIntelligenceTests(unittest.TestCase):
    def test_long_running_requires_sustained_cpu_not_one_hot_snapshot(self):
        samples = iter([
            {42: proc(cpu=12)},
            {42: proc(cpu=2)},
            {42: proc(cpu=1)},
        ])
        config = {
            "process_cpu_sample_count": 3,
            "process_cpu_sample_interval_seconds": 1,
            "process_io_enabled": False,
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
        }
        found = get_candidate_processes(
            config,
            snapshot_provider=lambda: next(samples),
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual([], found)

    def test_long_running_sustained_cpu_is_still_reported(self):
        samples = iter([
            {42: proc(cpu=12)},
            {42: proc(cpu=11)},
            {42: proc(cpu=10)},
        ])
        config = {
            "process_cpu_sample_count": 3,
            "process_cpu_sample_interval_seconds": 1,
            "process_io_enabled": False,
            "process_high_cpu_threshold": 50,
            "process_long_running_hours": 24,
            "process_long_running_min_cpu": 10,
        }
        found = get_candidate_processes(
            config,
            snapshot_provider=lambda: next(samples),
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(1, len(found))
        self.assertIn("across 3 samples", found[0]["reason"])

    def test_known_macos_activity_suppresses_normal_but_not_extreme_use(self):
        config = {
            "process_high_cpu_threshold": 50,
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_routine_review_multiplier": 4,
        }
        mds = proc(command="mds", comm="mds")
        mds["reason"] = (
            "Running 1-01:00:00 (limit 24h) with CPU at or above "
            "10.0% across 3 samples"
        )
        self.assertTrue(should_suppress_process_alert(mds, config))
        mds["reason"] = "CPU stayed at or above 50.0% for 3 samples over 60s"
        mds["cpu_samples"] = [75, 80, 70]
        self.assertTrue(should_suppress_process_alert(mds, config))
        mds["cpu_samples"] = [250, 240, 230]
        self.assertFalse(should_suppress_process_alert(mds, config))
        self.assertEqual("protected", process_action_policy(mds))

    def test_known_context_reuses_triage_and_recurrence_group(self):
        mds = proc(command="mds", comm="mds")
        guidance = known_process_guidance(mds)
        self.assertIn("Spotlight", guidance["role"])
        self.assertEqual("spotlight-indexing", guidance["recurrence_group"])
        triage = triage_process(mds, guidance, {}, recurrence=True)
        mds["resource_triage"] = triage
        prompt = investigation_prompt(None, mds, {})
        self.assertIn("Known macOS context", prompt)
        self.assertIn("Spotlight/Core Spotlight indexing", prompt)
        self.assertIn("Default handling", prompt)
        self.assertIn("Deterministic triage: review", prompt)
        self.assertIn("recurred within the review window", prompt)

    def test_known_background_first_io_incident_is_silent_but_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = []
            config = {
                "process_high_io_total_mib_per_second": 20,
                "process_high_io_write_mib_per_second": 10,
                "process_io_minimum_window_mib": 256,
                "process_io_required_intervals": 2,
                "system_disk_busy_mib_per_second": 50,
                "resource_monitor_interval_seconds": 10,
            }
            history_path = Path(directory) / "history.jsonl"
            monitor = ResourceMonitor(
                config,
                state_path=Path(directory) / "state.json",
                history_path=history_path,
                now_fn=lambda: 1000,
                notify_fn=lambda title, message: notifications.append((title, message)),
                identity_reader=lambda _pid: None,
            )
            p0 = proc(command="mds", comm="mds")
            p1 = proc(command="mds", comm="mds", read=160 * MIB, write=100 * MIB)
            p2 = proc(command="mds", comm="mds", read=320 * MIB, write=200 * MIB)
            status = {"available": True, "mib_per_second": 60, "error": ""}
            monitor.observe({42: p0}, {42: p1}, status, seconds=10, now=1010)
            monitor.observe({42: p1}, {42: p2}, status, seconds=10, now=1020)
            incident = monitor.state["incidents"][0]
            self.assertEqual("suppressed", incident["prompt_status"])
            self.assertEqual("suppress", incident["triage"]["decision"])
            self.assertEqual([], monitor.state["pending_prompts"])
            self.assertEqual([], notifications)
            self.assertIn("Spotlight", incident["known_process"]["role"])
            self.assertEqual(1, monitor.state["health"]["suppressed_incidents"])
            events = [json.loads(line)["event"] for line in history_path.read_text().splitlines()]
            self.assertEqual(["opened", "suppressed"], events[-2:])

    def test_known_recurrence_escalates_across_pid_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = []
            prompts = []
            current = {}
            config = {
                "process_high_io_total_mib_per_second": 20,
                "process_high_io_write_mib_per_second": 10,
                "process_io_minimum_window_mib": 256,
                "process_io_required_intervals": 2,
                "system_disk_busy_mib_per_second": 50,
                "resource_monitor_interval_seconds": 10,
                "resource_monitor_recovery_cool_samples": 1,
                "resource_monitor_recurrence_seconds": 30 * 60,
            }
            monitor = ResourceMonitor(
                config,
                state_path=Path(directory) / "state.json",
                history_path=Path(directory) / "history.jsonl",
                now_fn=lambda: 1000,
                notify_fn=lambda title, message: notifications.append((title, message)),
                prompt_fn=lambda process, incident: prompts.append((process, incident)) or "KEEP",
                identity_reader=lambda pid: current.get(pid),
            )
            status_hot = {"available": True, "mib_per_second": 60, "error": ""}
            status_cool = {"available": True, "mib_per_second": 1, "error": ""}

            p0 = proc(command="mds", comm="mds", start=100)
            p1 = proc(command="mds", comm="mds", read=160 * MIB, write=100 * MIB, start=100)
            p2 = proc(command="mds", comm="mds", read=320 * MIB, write=200 * MIB, start=100)
            current.clear(); current.update({42: p1})
            monitor.observe({42: p0}, {42: p1}, status_hot, seconds=10, now=1010)
            current.clear(); current.update({42: p2})
            monitor.observe({42: p1}, {42: p2}, status_hot, seconds=10, now=1020)
            cool = proc(command="mds", comm="mds", read=321 * MIB, write=200 * MIB, start=100)
            current.clear(); current.update({42: cool})
            monitor.observe({42: p2}, {42: cool}, status_cool, seconds=10, now=1030)
            self.assertEqual("recovered", monitor.state["incidents"][0]["status"])

            q0 = proc(43, command="mds --role changed", comm="mds", start=200)
            q1 = proc(43, command="mds --role changed", comm="mds", read=160 * MIB, write=100 * MIB, start=200)
            q2 = proc(43, command="mds --role changed", comm="mds", read=320 * MIB, write=200 * MIB, start=200)
            current.clear(); current.update({43: q1})
            monitor.observe({43: q0}, {43: q1}, status_hot, seconds=10, now=1340)
            current.clear(); current.update({43: q2})
            monitor.observe({43: q1}, {43: q2}, status_hot, seconds=10, now=1350)
            second = monitor.state["incidents"][-1]
            self.assertNotEqual(monitor.state["incidents"][0]["process_key"], second["process_key"])
            self.assertTrue(second["recurrence"])
            self.assertEqual("review", second["triage"]["decision"])
            self.assertEqual("completed", second["prompt_status"])
            self.assertEqual(1, len(notifications))
            self.assertEqual(1, len(prompts))

    def test_extreme_known_io_still_queues_review(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = []
            config = {
                "process_high_io_total_mib_per_second": 20,
                "process_high_io_write_mib_per_second": 10,
                "process_io_minimum_window_mib": 256,
                "process_io_required_intervals": 2,
                "system_disk_busy_mib_per_second": 50,
                "resource_monitor_interval_seconds": 10,
                "process_routine_review_multiplier": 4,
            }
            monitor = ResourceMonitor(
                config,
                state_path=Path(directory) / "state.json",
                history_path=Path(directory) / "history.jsonl",
                now_fn=lambda: 1000,
                notify_fn=lambda title, message: notifications.append((title, message)),
                identity_reader=lambda _pid: None,
            )
            p0 = proc(command="mds", comm="mds")
            p1 = proc(command="mds", comm="mds", read=500 * MIB, write=500 * MIB)
            p2 = proc(command="mds", comm="mds", read=1000 * MIB, write=1000 * MIB)
            status = {"available": True, "mib_per_second": 120, "error": ""}
            monitor.observe({42: p0}, {42: p1}, status, seconds=10, now=1010)
            monitor.observe({42: p1}, {42: p2}, status, seconds=10, now=1020)
            incident = monitor.state["incidents"][0]
            self.assertEqual("review", incident["triage"]["decision"])
            self.assertEqual("queued", incident["prompt_status"])
            self.assertEqual([incident["id"]], monitor.state["pending_prompts"])
            self.assertEqual(1, len(notifications))
            self.assertIn("needs review", notifications[0][0])

    def test_cool_monitor_iteration_skips_process_snapshot_and_idle_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Mock(return_value={})
            idle = Mock(return_value=999)
            fake_monitor = Mock()
            fake_monitor.interval_seconds = 10
            fake_monitor.idle_poll_seconds = 30
            fake_monitor.state = {
                "pending_prompts": [],
                "idle_armed": False,
            }
            config = {
                "resource_monitor_lock_path": str(Path(directory) / "monitor.lock"),
                "resource_monitor_interval_seconds": 10,
                "system_disk_busy_mib_per_second": 50,
            }
            with patch("resource_monitor.ResourceMonitor", return_value=fake_monitor):
                run_monitor(
                    config,
                    once=True,
                    snapshot_provider=snapshot,
                    disk_provider=lambda _seconds: {
                        "available": True,
                        "mib_per_second": 2,
                        "error": "",
                    },
                    idle_provider=idle,
                )
            snapshot.assert_not_called()
            idle.assert_not_called()
            fake_monitor.observe.assert_called_once()

    def test_state_heartbeat_writes_are_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            writes = []
            config = {
                "resource_monitor_interval_seconds": 10,
                "resource_monitor_state_flush_seconds": 30,
                "system_disk_busy_mib_per_second": 50,
            }
            monitor = ResourceMonitor(
                config,
                state_path=Path(directory) / "state.json",
                history_path=Path(directory) / "history.jsonl",
                now_fn=lambda: 0,
                notify_fn=lambda _title, _message: None,
            )
            with patch(
                "resource_monitor.atomic_write_json",
                side_effect=lambda path, state: writes.append(
                    (path, json.loads(json.dumps(state)))
                ) or True,
            ):
                cool = {"available": True, "mib_per_second": 1, "error": ""}
                monitor.observe({}, {}, cool, now=10)
                monitor.observe({}, {}, cool, now=20)
                monitor.observe({}, {}, cool, now=30)
                monitor.observe({}, {}, cool, now=40)
            self.assertEqual(2, len(writes))
            self.assertEqual(10, writes[0][1]["health"]["last_sample_at"])
            self.assertEqual(40, writes[1][1]["health"]["last_sample_at"])


if __name__ == "__main__":
    unittest.main()
