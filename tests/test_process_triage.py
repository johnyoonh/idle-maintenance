import json
import os
import tempfile
import unittest
from pathlib import Path

import process_identity as identity
from process_review import investigation_prompt, process_action_policy
from process_triage import candidates_requiring_review, routine_process_profile, triage_process
from resource_monitor import MIB, ResourceMonitor


def proc(
    pid=42,
    *,
    read=0,
    write=0,
    start=100,
    command="/usr/bin/sample --work",
    comm="/usr/bin/sample",
    cpu=1.0,
):
    value = {
        "pid": pid,
        "ppid": 1,
        "uid": os.getuid(),
        "cpu": cpu,
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


def mdworker(pid=42, **kwargs):
    return proc(
        pid,
        command="/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared",
        comm="/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared",
        **kwargs,
    )


class ProcessTriagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "process_high_cpu_threshold": 50,
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_routine_review_multiplier": 4,
        }

    def test_known_process_families_are_centralized_and_protected(self):
        cases = {
            "mdworker_shared": "spotlight-indexing",
            "mediaanalysisd": "photos-analysis",
            "fileproviderd": "cloud-sync",
            "contactsd": "contacts-and-suggestions",
            "backupd": "system-maintenance",
        }
        for name, family in cases.items():
            candidate = proc(command=f"/System/Library/{name}", comm=f"/System/Library/{name}")
            with self.subTest(name=name):
                self.assertEqual(routine_process_profile(candidate)["family"], family)
                self.assertEqual(process_action_policy(candidate), "protected")

    def test_routine_process_is_suppressed_below_review_ceiling(self):
        candidate = mdworker()
        decision = triage_process(
            candidate,
            self.config,
            peak_total_mib_s=30,
            peak_write_mib_s=12,
        )
        self.assertEqual(decision["decision"], "suppress")
        self.assertEqual(decision["classification"], "routine-known")
        self.assertEqual(decision["profile"]["family"], "spotlight-indexing")

    def test_recurrence_and_extreme_use_escalate_known_process(self):
        candidate = mdworker()
        recurrent = triage_process(candidate, self.config, recurrence=True)
        extreme = triage_process(candidate, self.config, peak_total_mib_s=81)
        self.assertEqual(recurrent["decision"], "review")
        self.assertIn("recurred", recurrent["reason"])
        self.assertEqual(extreme["decision"], "review")
        self.assertIn("review ceiling", extreme["reason"])

    def test_unknown_process_keeps_existing_review_behavior(self):
        decision = triage_process(proc(), self.config)
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["classification"], "unknown")

    def test_manual_audit_uses_same_triage_policy(self):
        routine = mdworker(cpu=75)
        routine["cpu_samples"] = [75, 80, 70]
        unknown = proc(pid=43, cpu=75)
        unknown["cpu_samples"] = [75, 80, 70]
        extreme = mdworker(pid=44, cpu=250)
        extreme["cpu_samples"] = [250, 250, 250]
        result = candidates_requiring_review([routine, unknown, extreme], self.config)
        self.assertEqual([item["pid"] for item in result], [43, 44])

    def test_investigation_prompt_explains_known_context_and_default_action(self):
        candidate = mdworker()
        candidate["resource_triage"] = triage_process(candidate, self.config, recurrence=True)
        prompt = investigation_prompt(None, candidate, self.config)
        self.assertIn("Known macOS context", prompt)
        self.assertIn("Spotlight/search indexing", prompt)
        self.assertIn("Default action: Leave it running", prompt)
        self.assertIn("Current deterministic triage: review", prompt)
        self.assertIn("recurred within the review window", prompt)


class ResourceMonitorTriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.history_path = self.root / "history.jsonl"
        self.clock = 1_800_000_000.0
        self.notifications = []
        self.prompts = []
        self.current = {}
        self.config = {
            "process_high_cpu_threshold": 50,
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_io_minimum_window_mib": 256,
            "process_io_required_intervals": 2,
            "process_routine_review_multiplier": 4,
            "system_disk_busy_mib_per_second": 50,
            "resource_monitor_interval_seconds": 10,
            "resource_monitor_recovery_cool_samples": 1,
            "resource_monitor_notification_cooldown_seconds": 6 * 3600,
            "resource_monitor_recurrence_seconds": 30 * 60,
            "resource_monitor_incident_limit": 20,
            "resource_monitor_history_limit": 50,
            "return_from_away_minutes": 15,
        }
        self.monitor = ResourceMonitor(
            self.config,
            state_path=self.state_path,
            history_path=self.history_path,
            now_fn=lambda: self.clock,
            notify_fn=lambda title, message: self.notifications.append((title, message)),
            prompt_fn=lambda process, incident: self.prompts.append((process, incident)) or "KEEP",
            identity_reader=lambda pid: self.current.get(pid),
        )

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, previous, current, *, system=60, advance=10):
        self.clock += advance
        self.current = current
        self.monitor.observe(
            previous,
            current,
            {"available": True, "mib_per_second": system, "error": ""},
            seconds=10,
            idle_seconds=0,
            now=self.clock,
        )

    def open_routine_incident(self, *, pid=42, start=100):
        p0 = mdworker(pid, read=0, write=0, start=start)
        p1 = mdworker(pid, read=160 * MIB, write=100 * MIB, start=start)
        p2 = mdworker(pid, read=320 * MIB, write=200 * MIB, start=start)
        self.observe({pid: p0}, {pid: p1})
        self.observe({pid: p1}, {pid: p2})
        return p2, self.monitor.state["incidents"][-1]

    def test_first_routine_incident_is_recorded_without_alarm(self):
        _current, incident = self.open_routine_incident()
        self.assertEqual(incident["prompt_status"], "suppressed")
        self.assertEqual(incident["triage"]["decision"], "suppress")
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.monitor.state["pending_prompts"], [])
        self.assertEqual(self.monitor.state["health"]["suppressed_incidents"], 1)
        events = [json.loads(line)["event"] for line in self.history_path.read_text().splitlines()]
        self.assertEqual(events[-2:], ["opened", "suppressed"])

    def test_same_routine_process_recurrence_escalates_across_pid_restart(self):
        current, first = self.open_routine_incident()
        cool = mdworker(42, read=current["io_read_bytes"] + MIB, write=current["io_write_bytes"], start=100)
        self.observe({42: current}, {42: cool}, system=10)
        self.assertEqual(first["status"], "recovered")

        self.clock += 5 * 60
        p0 = mdworker(43, read=0, write=0, start=200)
        p1 = mdworker(43, read=160 * MIB, write=100 * MIB, start=200)
        p2 = mdworker(43, read=320 * MIB, write=200 * MIB, start=200)
        self.observe({43: p0}, {43: p1})
        self.observe({43: p1}, {43: p2})

        second = self.monitor.state["incidents"][-1]
        self.assertTrue(second["recurrence"])
        self.assertEqual(second["triage"]["decision"], "review")
        self.assertEqual(second["prompt_status"], "completed")
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.prompts), 1)

    def test_routine_family_recurrence_survives_command_fingerprint_change(self):
        current, first = self.open_routine_incident()
        cool = mdworker(42, read=current["io_read_bytes"] + MIB, write=current["io_write_bytes"], start=100)
        self.observe({42: current}, {42: cool}, system=10)
        self.assertEqual(first["status"], "recovered")

        self.clock += 5 * 60
        command = "/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared --role changed"
        p0 = proc(43, read=0, write=0, start=200, command=command, comm=command.split()[0])
        p1 = proc(43, read=160 * MIB, write=100 * MIB, start=200, command=command, comm=command.split()[0])
        p2 = proc(43, read=320 * MIB, write=200 * MIB, start=200, command=command, comm=command.split()[0])
        self.observe({43: p0}, {43: p1})
        self.observe({43: p1}, {43: p2})

        second = self.monitor.state["incidents"][-1]
        self.assertNotEqual(first["process_key"], second["process_key"])
        self.assertTrue(second["recurrence"])
        self.assertEqual(second["triage"]["decision"], "review")

    def test_extreme_known_process_still_raises_review(self):
        p0 = mdworker(read=0, write=0)
        p1 = mdworker(read=500 * MIB, write=500 * MIB)
        p2 = mdworker(read=1000 * MIB, write=1000 * MIB)
        self.observe({42: p0}, {42: p1})
        self.observe({42: p1}, {42: p2})
        incident = self.monitor.state["incidents"][-1]
        self.assertEqual(incident["triage"]["decision"], "review")
        self.assertEqual(incident["prompt_status"], "queued")
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.monitor.state["pending_prompts"], [incident["id"]])


if __name__ == "__main__":
    unittest.main()
