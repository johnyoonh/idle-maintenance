#!/usr/bin/env python3
"""Portable, fully mocked tests for the macOS Time Machine policy worker."""
from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import time_machine_backup as tm


EASTERN = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 22, 22, 0, tzinfo=EASTERN)
OLD = "/Volumes/Backup/2026-10-19-120000.backup"
NEW = "/Volumes/Backup/2026-10-22-220500.backup"


class FakeRunner:
    def __init__(self, latest=None, on_ac=True, destination=True,
                 running=False, result=0):
        self.latest = list(latest if latest is not None else [OLD, NEW])
        self.on_ac = on_ac
        self.destination = destination
        self.running = running
        self.result = result
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        action = argv[1]
        if argv[0] == tm.PMSET:
            return subprocess.CompletedProcess(argv, 0, "Now drawing from 'AC Power'\n"
                                               if self.on_ac else "Now drawing from 'Battery Power'\n", "")
        if action == "latestbackup":
            value = self.latest.pop(0) if self.latest else None
            return subprocess.CompletedProcess(argv, 0 if value else 1,
                                               (value + "\n") if value else "", "")
        if action == "destinationinfo":
            return subprocess.CompletedProcess(argv, 0, "Name : Backup\nID : abcd\n"
                                               if self.destination else "", "")
        if action == "status":
            output = ("Backup session status:\n{ Running = 1; }\n" if self.running
                      else "Backup session status:\n{ Running = 0; }\n")
            if self.running is None:
                output = "Backup session status: {unknown}"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if action == "startbackup":
            return subprocess.CompletedProcess(argv, self.result, "", "backup failed" if self.result else "")
        raise AssertionError(f"Unexpected command: {argv}")


class TimeMachinePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "time-machine.json"
        self.lock = root / "worker.lock"
        self.runner = FakeRunner()
        self.cfg = {"time_machine_backup": {"enabled": True}}
        self.alerts = []
        self.kwargs = dict(
            config=self.cfg,
            state_file=self.state,
            lock_file=self.lock,
            runner=self.runner,
            clock=lambda: NOW,
            idle_reader=lambda: 1900,
            load_reader=lambda: (0.4, 0.3, 0.2),
            disk_probe=lambda _cfg: {"available": True, "busy": False},
            notifier=lambda title, message: self.alerts.append((title, message)),
        )

    def run_policy(self, **overrides):
        return tm.run_once(**(self.kwargs | overrides))

    def test_opt_in_required_no_system_commands(self):
        self.assertEqual("disabled", self.run_policy(config={})["status"])
        self.assertEqual([], self.runner.calls)
        self.assertFalse(self.state.exists())

    def test_overnight_window_and_equal_hour_meaning(self):
        for hour in (20, 23, 0, 6):
            self.assertTrue(tm.within_window(hour, 20, 7))
        for hour in (7, 14, 19):
            self.assertFalse(tm.within_window(hour, 20, 7))
        self.assertTrue(tm.within_window(11, 11, 11))
        self.assertTrue(tm.within_window(12, 9, 17))
        self.assertFalse(tm.within_window(20, 9, 17))

    def test_config_validation(self):
        for cfg in ({"time_machine_backup": {"enabled": "true"}},
                    {"time_machine_backup": {"start_hour": 24}},
                    {"time_machine_backup": {"idle_minutes": -1}},
                    {"time_machine_backup": {"max_load_1min": 0}}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                tm.policy_from(cfg)

    def test_latest_backup_timestamp_and_running_parser(self):
        self.assertEqual(
            datetime(2026, 10, 20, 15, 22, 1, tzinfo=EASTERN).timestamp(),
            tm.backup_epoch("/Volumes/Disk/2026-10-20-152201.backup", EASTERN),
        )
        self.assertIsNone(tm.backup_epoch("some/malformed/path", EASTERN))
        self.assertTrue(tm.parse_running("{ Running = 1; }"))
        self.assertFalse(tm.parse_running("{ Running = 0; }"))
        self.assertIsNone(tm.parse_running("unexpected output"))

    def test_outside_window(self):
        result = self.run_policy(clock=lambda: NOW.replace(hour=12))
        self.assertEqual("outside_window", result["reason"])
        self.assertFalse(any(x[0][1] == "startbackup" for x in self.runner.calls))

    def test_recent_native_backup_prevents_duplicate(self):
        self.runner.latest = ["/Volumes/Backup/2026-10-22-203000.backup"]
        self.assertEqual("backup_recent", self.run_policy()["reason"])
        self.assertFalse(any(x[0][1] == "startbackup" for x in self.runner.calls))

    def test_inactive_requirement_is_fail_closed(self):
        self.assertEqual("not_idle", self.run_policy(idle_reader=lambda: 60)["reason"])
        self.runner = FakeRunner()
        self.kwargs["runner"] = self.runner
        self.assertEqual("not_idle", self.run_policy(idle_reader=lambda: None)["reason"])

    def test_ac_only(self):
        self.runner.on_ac = False
        self.assertEqual("not_on_ac", self.run_policy()["reason"])

    def test_cpu_and_disk_gates(self):
        self.runner.latest = [OLD]
        self.assertEqual(
            "cpu_busy", self.run_policy(load_reader=lambda: (5.0, 0, 0))["reason"]
        )
        self.assertEqual(
            "disk_busy_or_unknown",
            self.run_policy(disk_probe=lambda _cfg: {"available": True, "busy": True})["reason"],
        )
        self.assertEqual(
            "disk_busy_or_unknown",
            self.run_policy(disk_probe=lambda _cfg: {"available": False})["reason"],
        )

    def test_destination_and_native_status_are_fail_closed(self):
        self.runner.latest = [OLD]
        self.runner.destination = False
        self.assertEqual("destination_unavailable", self.run_policy()["reason"])
        self.runner.destination = True
        self.runner.running = True
        self.assertEqual("backup_running_or_unknown", self.run_policy()["reason"])
        self.runner.running = None
        self.assertEqual("backup_running_or_unknown", self.run_policy()["reason"])

    def test_success_starts_native_auto_mode_and_verifies_new_backup(self):
        result = self.run_policy()
        self.assertEqual("success", result["status"])
        self.assertEqual("verified_new_backup", result["reason"])
        starts = [(argv, options) for argv, options in self.runner.calls
                  if argv[1] == "startbackup"]
        self.assertEqual(1, len(starts))
        self.assertEqual(
            [tm.TMUTIL, "startbackup", "--auto", "--block"], starts[0][0]
        )
        self.assertEqual(tm.BACKUP_TIMEOUT, starts[0][1]["timeout"])
        state = json.loads(self.state.read_text())
        self.assertEqual("success", state["last_result"])
        self.assertEqual(NEW, state["last_backup_path"])
        self.assertEqual(NOW.timestamp(), state["last_success_at"])
        self.assertEqual(0o600, self.state.stat().st_mode & 0o777)

    def test_start_failure_and_three_hour_retry_cooldown(self):
        self.runner.result = 1
        self.assertEqual("backup_not_verified", self.run_policy()["reason"])
        next_runner = FakeRunner(latest=[OLD])
        result = self.run_policy(
            runner=next_runner, clock=lambda: NOW + timedelta(minutes=15)
        )
        self.assertEqual("retry_cooldown", result["reason"])
        self.assertFalse(any(c[0][1] == "startbackup" for c in next_runner.calls))

    def test_zero_exit_without_new_backup_is_not_success(self):
        self.runner.latest = [OLD, OLD]
        self.assertEqual("backup_not_verified", self.run_policy()["reason"])
        self.assertEqual("failed", json.loads(self.state.read_text())["last_result"])

    def test_overdue_alert_is_rate_limited_even_when_daytime(self):
        noon = NOW.replace(hour=12)
        self.runner.latest = [OLD]
        self.run_policy(clock=lambda: noon)
        self.assertEqual(1, len(self.alerts))
        self.runner.latest = [OLD]
        self.run_policy(clock=lambda: noon + timedelta(minutes=30))
        self.assertEqual(1, len(self.alerts))

    def test_no_previous_backup_alerts_only_after_48h(self):
        self.runner.latest = [None]
        self.run_policy(clock=lambda: NOW.replace(hour=12))
        self.assertEqual([], self.alerts)
        self.runner.latest = [None]
        self.run_policy(clock=lambda: NOW.replace(hour=12) + timedelta(hours=49))
        self.assertEqual(1, len(self.alerts))

    def test_file_lock_prevents_parallel_backups(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual("worker_already_running", self.run_policy()["reason"])
            self.assertEqual([], self.runner.calls)
            fcntl.flock(held, fcntl.LOCK_UN)


if __name__ == "__main__":
    unittest.main()
