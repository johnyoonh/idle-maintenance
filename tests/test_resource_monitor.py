import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import maintenance_status
import process_identity as identity
from process_review import ATTRIBUTION_NOTE, investigation_prompt, process_action_policy, terminate
from resource_monitor import MIB, ResourceMonitor, append_bounded_jsonl, process_instance_id, rolling_delta


def proc(pid=42, *, read=0, write=0, start=100, command="/usr/bin/sample --work", comm="/usr/bin/sample"):
    value = {
        "pid": pid,
        "ppid": 1,
        "uid": os.getuid(),
        "cpu": 1.0,
        "etime": "00:10:00",
        "elapsed_seconds": 600,
        "start_time": "Tue Jul 28 20:00:00 2026",
        "start_abstime": start,
        "comm": comm,
        "command": command,
        "fingerprint": identity.fingerprint(command),
        "io_read_bytes": read,
        "io_write_bytes": write,
    }
    value["process_key"] = identity.key(value)
    return value


class ResourceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.history = self.root / "history.jsonl"
        self.notifications = []
        self.prompts = []
        self.current = proc()
        self.config = {
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_io_minimum_window_mib": 256,
            "process_io_required_intervals": 2,
            "system_disk_busy_mib_per_second": 50,
            "resource_monitor_interval_seconds": 10,
            "resource_monitor_recovery_cool_samples": 6,
            "resource_monitor_notification_cooldown_seconds": 6 * 3600,
            "resource_monitor_recurrence_seconds": 30 * 60,
            "review_prompt_idle_seconds": 30,
            "review_prompt_idle_max_seconds": 300,
            "resource_monitor_incident_limit": 4,
            "resource_monitor_history_limit": 5,
            "return_from_away_minutes": 15,
        }
        self.clock = 1_800_000_000.0
        self.monitor = ResourceMonitor(
            self.config,
            state_path=self.state,
            history_path=self.history,
            now_fn=lambda: self.clock,
            notify_fn=lambda title, message: self.notifications.append((title, message)),
            prompt_fn=lambda process, incident: self.prompts.append((process, incident)) or "KEEP",
            identity_reader=lambda _pid: self.current,
        )

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, previous, current, *, system=60, idle=0, advance=10):
        self.clock += advance
        self.current = current.get(42, self.current)
        self.monitor.observe(
            previous,
            current,
            {"available": True, "mib_per_second": system, "error": ""},
            seconds=10,
            idle_seconds=idle,
            now=self.clock,
        )

    def test_default_notification_attaches_incident_history(self):
        monitor = ResourceMonitor(
            self.config,
            state_path=self.state,
            history_path=self.history,
            now_fn=lambda: self.clock,
        )

        with patch("maintenance_core.notify_user") as notify:
            monitor._notify_default("Idle Maintenance resource incident", "synthetic incident")

        notify.assert_called_once_with(
            "Idle Maintenance resource incident",
            "synthetic incident",
            click_path=self.history,
        )

    def open_incident(self):
        p0 = proc(read=0, write=0)
        p1 = proc(read=160 * MIB, write=100 * MIB)
        p2 = proc(read=320 * MIB, write=200 * MIB)
        self.observe({42: p0}, {42: p1})
        self.observe({42: p1}, {42: p2})
        self.assertEqual(len(self.monitor.state["incidents"]), 1)
        return p2, self.monitor.state["incidents"][0]

    def test_prompt_failure_is_persisted_in_monitor_health(self):
        incident = {
            "id": "prompt-failure",
            "pid": 42,
            "process": "sample",
            "process_identity": process_instance_id(self.current),
            "process_snapshot": self.current,
            "prompt_status": "queued",
        }
        self.monitor.state["incidents"] = [incident]
        self.monitor.state["pending_prompts"] = [incident["id"]]
        self.monitor.prompt_fn = lambda _process, _incident: (_ for _ in ()).throw(
            RuntimeError("synthetic prompt failure")
        )

        self.monitor._deliver_prompt(incident, self.clock)

        self.assertEqual("failed", incident["prompt_status"])
        self.assertEqual("synthetic prompt failure", incident["prompt_error"])
        self.assertEqual("synthetic prompt failure", self.monitor.state["prompt_health"]["last_error"])
        self.assertEqual([], self.monitor.state["pending_prompts"])

    def cool_and_recover(self, previous, samples=6):
        current = previous
        for _ in range(samples):
            next_value = proc(
                read=current["io_read_bytes"] + MIB,
                write=current["io_write_bytes"],
            )
            self.observe({42: current}, {42: next_value}, system=10)
            current = next_value
        return current

    def test_rolling_delta_and_counter_reset(self):
        first = proc(read=10 * MIB, write=5 * MIB)
        second = proc(read=110 * MIB, write=55 * MIB)
        delta = rolling_delta(first, second, 10)
        self.assertAlmostEqual(delta["total_mib_s"], 15.0)
        self.assertAlmostEqual(delta["write_mib_s"], 5.0)
        self.assertIsNone(rolling_delta(second, first, 10))

    def test_pid_reuse_discards_interval(self):
        first = proc(read=0, write=0, start=100)
        reused = proc(read=500 * MIB, write=500 * MIB, start=200)
        self.assertIsNone(rolling_delta(first, reused, 10))
        self.observe({42: first}, {42: reused})
        self.assertEqual(self.monitor.state["incidents"], [])

    def test_aggregate_gate_blocks_process_thresholds(self):
        p0 = proc()
        p1 = proc(read=160 * MIB, write=100 * MIB)
        p2 = proc(read=320 * MIB, write=200 * MIB)
        self.observe({42: p0}, {42: p1}, system=49.9)
        self.observe({42: p1}, {42: p2}, system=49.9)
        self.assertEqual(self.monitor.state["incidents"], [])
        self.assertEqual(self.monitor._windows, {})

    def test_process_churn_keeps_transient_windows_bounded(self):
        for index in range(2_000):
            pid = 1_000 + index
            before = proc(pid=pid, start=10_000 + index)
            after = proc(pid=pid, start=10_000 + index, read=MIB)
            self.observe({pid: before}, {pid: after})
            self.assertLessEqual(len(self.monitor._windows), 1)

        self.observe({}, {})

        self.assertEqual(self.monitor._windows, {})
        self.assertEqual(self.monitor.state["health"]["tracked_windows"], 0)
        self.assertEqual(self.monitor.state["health"]["sampled_processes"], 0)

    def test_cool_sample_clears_transient_windows(self):
        p0 = proc()
        p1 = proc(read=MIB)
        p2 = proc(read=2 * MIB)
        self.observe({42: p0}, {42: p1})
        self.assertEqual(len(self.monitor._windows), 1)

        self.observe({42: p1}, {42: p2}, system=10)

        self.assertEqual(self.monitor._windows, {})
        self.assertEqual(self.monitor.state["health"]["tracked_windows"], 0)

    def test_sustained_threshold_and_minimum_window_open_incident(self):
        _current, incident = self.open_incident()
        self.assertEqual(incident["status"], "active")
        self.assertGreaterEqual(incident["window_bytes"], 256 * MIB)
        self.assertIn("not definitive physical-disk attribution", incident["attribution"])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.monitor.state["pending_prompts"], [incident["id"]])
        self.assertEqual(self.monitor._windows, {})

    def test_recovery_requires_six_cool_samples(self):
        current, incident = self.open_incident()
        current = self.cool_and_recover(current, samples=5)
        self.assertEqual(incident["status"], "active")
        current = self.cool_and_recover(current, samples=1)
        self.assertEqual(incident["status"], "recovered")
        self.assertEqual(self.monitor.state["active"], {})
        self.assertEqual(incident["prompt_status"], "cancelled")
        self.assertEqual(self.monitor.state["pending_prompts"], [])

    def test_notification_dedupe_and_six_hour_cooldown(self):
        current, first = self.open_incident()
        current = self.cool_and_recover(current)
        self.clock += 60
        p1 = proc(read=current["io_read_bytes"] + 160 * MIB, write=current["io_write_bytes"] + 100 * MIB)
        p2 = proc(read=p1["io_read_bytes"] + 160 * MIB, write=p1["io_write_bytes"] + 100 * MIB)
        self.observe({42: current}, {42: p1})
        self.observe({42: p1}, {42: p2})
        self.assertEqual(len(self.notifications), 1)
        self.clock += 6 * 3600
        current = self.cool_and_recover(p2)
        p3 = proc(read=current["io_read_bytes"] + 160 * MIB, write=current["io_write_bytes"] + 100 * MIB)
        p4 = proc(read=p3["io_read_bytes"] + 160 * MIB, write=p3["io_write_bytes"] + 100 * MIB)
        self.observe({42: current}, {42: p3})
        self.observe({42: p3}, {42: p4})
        self.assertEqual(len(self.notifications), 2)

    def test_second_incident_within_thirty_minutes_is_queued(self):
        current, first = self.open_incident()
        current = self.cool_and_recover(current)
        self.clock += 5 * 60
        p1 = proc(read=current["io_read_bytes"] + 160 * MIB, write=current["io_write_bytes"] + 100 * MIB)
        p2 = proc(read=p1["io_read_bytes"] + 160 * MIB, write=p1["io_write_bytes"] + 100 * MIB)
        self.observe({42: current}, {42: p1})
        self.observe({42: p1}, {42: p2})
        second = self.monitor.state["incidents"][-1]
        self.assertTrue(second["recurrence"])
        self.assertEqual(second["prompt_status"], "queued")
        self.assertEqual(self.monitor.state["pending_prompts"], [second["id"]])
        self.assertEqual(self.prompts, [])

    def test_queued_prompt_waits_for_quiet_idle_window(self):
        current, incident = self.open_incident()
        self.assertEqual(self.prompts, [])
        next_value = proc(read=current["io_read_bytes"] + MIB, write=current["io_write_bytes"])
        self.observe({42: current}, {42: next_value}, system=10, idle=901)
        self.assertEqual(self.prompts, [])
        final = proc(read=next_value["io_read_bytes"] + MIB, write=next_value["io_write_bytes"])
        self.observe({42: next_value}, {42: final}, system=10, idle=0)
        self.assertEqual(self.prompts, [])
        quiet = proc(read=final["io_read_bytes"] + MIB, write=final["io_write_bytes"])
        self.observe({42: final}, {42: quiet}, system=10, idle=30)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(incident["prompt_status"], "completed")

    def test_delivery_refuses_recovered_incident(self):
        _current, incident = self.open_incident()
        incident["status"] = "recovered"

        self.monitor._deliver_prompt(incident, self.clock)

        self.assertEqual(self.prompts, [])
        self.assertEqual(incident["prompt_status"], "cancelled")
        self.assertEqual(self.monitor.state["pending_prompts"], [])

    def test_atomic_persistence_and_bounded_retention(self):
        self.open_incident()
        loaded = json.loads(self.state.read_text())
        self.assertEqual(loaded["schema_version"], 1)
        self.assertNotIn("windows", loaded)
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))
        for index in range(10):
            append_bounded_jsonl(self.history, {"index": index}, 5)
        rows = [json.loads(line) for line in self.history.read_text().splitlines()]
        self.assertEqual([row["index"] for row in rows], [5, 6, 7, 8, 9])
        self.monitor.state["incidents"] = [
            {"id": str(index), "last_seen_at": index} for index in range(10)
        ]
        self.monitor._prune(self.clock)
        self.assertEqual([item["id"] for item in self.monitor.state["incidents"]], ["6", "7", "8", "9"])

    def test_legacy_persisted_windows_are_discarded_and_compacted(self):
        legacy_state = {
            "schema_version": 1,
            "health": {},
            "incidents": [],
            "active": {},
            "notifications": {},
            "pending_prompts": [],
            "windows": {
                "stale-process": [
                    {"read_bytes": MIB, "write_bytes": 0, "total_bytes": MIB}
                ]
            },
            "idle_armed": False,
        }
        self.state.write_text(json.dumps(legacy_state))

        monitor = ResourceMonitor(
            self.config,
            state_path=self.state,
            history_path=self.history,
            now_fn=lambda: self.clock,
        )
        monitor._persist(force=True, now=self.clock)

        self.assertNotIn("windows", monitor.state)
        self.assertEqual(monitor._windows, {})
        self.assertNotIn("windows", json.loads(self.state.read_text()))

    def test_historical_alerts_remain_separate_from_live_queue(self):
        _current, incident = self.open_incident()
        self.assertEqual(self.monitor.state["pending_prompts"], [incident["id"]])
        self.monitor.state["pending_prompts"].clear()
        self.assertEqual(len(self.monitor.state["incidents"]), 1)
        self.assertTrue(self.history.exists())


class ProcessPolicyTests(unittest.TestCase):
    def test_protected_apple_daemons_have_no_destructive_policy(self):
        for name in ("mediaanalysisd", "photoanalysisd", "contactsd", "corespotlightd", "mds", "mdworker_shared", "fileproviderd"):
            self.assertEqual(process_action_policy(proc(comm=f"/System/Library/{name}", command=name)), "protected")

    def test_mail_and_main_shortcuts_get_graceful_quit(self):
        mail = proc(comm="/System/Applications/Mail.app/Contents/MacOS/Mail", command="/System/Applications/Mail.app/Contents/MacOS/Mail")
        shortcuts = proc(comm="/System/Applications/Shortcuts.app/Contents/MacOS/Shortcuts", command="/System/Applications/Shortcuts.app/Contents/MacOS/Shortcuts")
        helper = proc(comm="ShortcutsEvents", command="/System/Library/ShortcutsEvents")
        self.assertEqual(process_action_policy(mail), "graceful-quit")
        self.assertEqual(process_action_policy(shortcuts), "graceful-quit")
        self.assertEqual(process_action_policy(helper), "review-only")

    def test_graceful_quit_revalidates_and_uses_sigterm_only(self):
        expected = proc()
        signaler = Mock()
        identities = iter([expected, None])
        result = terminate(
            expected,
            {"process_terminate_grace_seconds": 1, "process_terminate_poll_seconds": 0.1},
            identity_provider=lambda _pid: next(identities),
            signal_fn=signaler,
            sleep_fn=lambda _seconds: None,
            monotonic=Mock(side_effect=[0, 0]),
        )
        self.assertEqual(result, "terminated")
        signaler.assert_called_once_with(42, signal.SIGTERM)

    def test_investigation_prompt_has_safety_boundary(self):
        prompt = investigation_prompt(None, proc(), {})
        self.assertNotIn("fs_usage", prompt)
        self.assertNotIn("sudo", prompt)
        self.assertIn(ATTRIBUTION_NOTE, prompt)
        self.assertIn("rootless", prompt)


class StatusTests(unittest.TestCase):
    def test_status_degrades_when_prompt_delivery_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            support = home / "Library/Application Support/idle-maintenance"
            support.mkdir(parents=True)
            now = 1_800_000_000
            (support / "resource-monitor-state.json").write_text(json.dumps({
                "schema_version": 1,
                "health": {
                    "last_sample_at": now - 5,
                    "sample_interval_seconds": 10,
                    "last_error": "",
                    "last_prompt_error": "synthetic prompt failure",
                },
            }))

            status = maintenance_status.resource_monitor_status(
                support,
                {"healthy": True, "state": "running"},
                now,
            )

            self.assertFalse(status["healthy"])
            self.assertEqual("degraded", status["state"])
            self.assertEqual("synthetic prompt failure", status["last_prompt_error"])

    def test_status_json_exposes_monitor_health_and_incidents(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            support = home / "Library/Application Support/idle-maintenance"
            support.mkdir(parents=True)
            logs = home / "Library/Logs/wiki-automation"
            logs.mkdir(parents=True)
            now = 1_800_000_000
            state = {
                "schema_version": 1,
                "health": {
                    "last_sample_at": now - 5,
                    "sample_interval_seconds": 10,
                    "last_system_mib_s": 61.2,
                    "last_error": "",
                    "sampled_processes": 12,
                    "tracked_windows": 3,
                },
                "incidents": [{"id": "one", "process": "sample", "pid": 42, "status": "active", "peak_total_mib_s": 25, "last_seen_at": now}],
                "active": {"identity": "one"},
                "pending_prompts": ["one"],
            }
            (support / "resource-monitor-state.json").write_text(json.dumps(state))

            def runner(command, **_kwargs):
                if command[0] == "launchctl":
                    return subprocess.CompletedProcess(command, 0, stdout="state = running\nlast exit code = 0\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="runner ok", stderr="")

            status = maintenance_status.collect_status(
                config={
                    "scheduled_runner_status_command": ["~/runner", "--status"],
                    "terminal_suggestion_start_hour": 9,
                    "terminal_suggestion_end_hour": 21,
                },
                command_runner=runner,
                home=home,
                now=now,
            )
            self.assertTrue(status["resource_monitor"]["healthy"])
            self.assertEqual(status["resource_monitor"]["active_incidents"], 1)
            self.assertEqual(status["resource_monitor"]["sampled_processes"], 12)
            self.assertEqual(status["resource_monitor"]["tracked_windows"], 3)
            self.assertEqual(status["resource_monitor"]["recent_incidents"][0]["id"], "one")
            self.assertIn("attribution_boundary", status["resource_monitor"])
            json.dumps(status)


if __name__ == "__main__":
    unittest.main()
