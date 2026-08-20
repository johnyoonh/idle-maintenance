import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance_status
import resource_monitor
from resource_monitor import ResourceMonitor


class ReturnFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "resource_monitor_interval_seconds": 10,
            "resource_monitor_idle_poll_seconds": 30,
            "resource_monitor_state_flush_seconds": 30,
            "resource_monitor_incident_limit": 10,
            "resource_monitor_history_limit": 10,
            "resource_monitor_notification_cooldown_seconds": 3600,
            "resource_monitor_recovery_cool_samples": 6,
            "system_disk_busy_mib_per_second": 50,
            "idle_threshold_minutes": 10,
            "return_from_away_minutes": 15,
            "post_trigger_cooldown_seconds": 3600,
        }
        self.events = []
        self.monitor = ResourceMonitor(
            self.config,
            state_path=self.root / "state.json",
            history_path=self.root / "history.jsonl",
            notify_fn=lambda *_args: None,
            return_fn=lambda: self.events.append("return") or {"ok": True},
        )

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, *, idle, now):
        self.monitor.observe(
            {},
            {},
            {"available": True, "mib_per_second": 0, "error": ""},
            seconds=10,
            idle_seconds=idle,
            now=now,
        )

    def test_return_flow_runs_without_pending_process_prompt(self):
        self.observe(idle=601, now=1000)
        self.assertTrue(self.monitor.state["return_armed"])
        self.assertEqual(self.events, [])

        self.observe(idle=0, now=1010)

        self.assertEqual(self.events, ["return"])
        self.assertFalse(self.monitor.state["return_armed"])
        self.assertEqual(self.monitor.state["last_return_flow_at"], 1010)
        self.assertEqual(self.monitor.state["return_health"]["last_success_at"], 1010)

    def test_process_prompt_precedes_resume_router(self):
        incident = {"id": "queued"}
        self.monitor.state["incidents"] = [incident]
        self.monitor.state["pending_prompts"] = [incident["id"]]

        def deliver(current, _now):
            self.events.append("prompt")
            self.monitor.state["pending_prompts"].remove(current["id"])

        self.monitor._deliver_prompt = deliver

        self.observe(idle=901, now=2000)
        self.assertTrue(self.monitor.state["idle_armed"])
        self.assertTrue(self.monitor.state["return_armed"])

        self.observe(idle=0, now=2010)

        self.assertEqual(self.events, ["prompt", "return"])

    def test_cooldown_requires_another_away_return_cycle(self):
        self.observe(idle=601, now=3000)
        self.observe(idle=0, now=3010)
        self.observe(idle=601, now=3500)
        self.observe(idle=0, now=3510)
        self.assertEqual(self.events, ["return"])

        self.observe(idle=601, now=7000)
        self.observe(idle=0, now=7010)
        self.assertEqual(self.events, ["return", "return"])

    def test_return_failure_is_recorded_without_raising(self):
        def fail():
            raise RuntimeError("synthetic return failure")

        self.monitor.return_fn = fail
        self.observe(idle=601, now=8000)
        self.observe(idle=0, now=8010)

        health = self.monitor.state["return_health"]
        self.assertEqual(health["last_error"], "synthetic return failure")
        self.assertEqual(health["last_error_at"], 8010)

    def test_run_monitor_polls_idle_without_pending_prompts(self):
        observed = []
        idle_polls = []

        class FakeMonitor:
            interval_seconds = 10
            idle_poll_seconds = 30
            state = {}

            def observe(self, _previous, _current, _status, **kwargs):
                observed.append(kwargs["idle_seconds"])

        config = {
            "resource_monitor_lock_path": str(self.root / "monitor.lock"),
            "system_disk_busy_mib_per_second": 50,
        }
        with patch.object(resource_monitor, "ResourceMonitor", return_value=FakeMonitor()):
            result = resource_monitor.run_monitor(
                config,
                once=True,
                snapshot_provider=lambda: {},
                disk_provider=lambda _seconds: {
                    "available": True,
                    "mib_per_second": 0,
                    "error": "",
                },
                idle_provider=lambda: idle_polls.append(True) or 777,
                monotonic_fn=lambda: 1,
            )

        self.assertEqual(result, 0)
        self.assertEqual(idle_polls, [True])
        self.assertEqual(observed, [777])

    def test_status_degrades_when_resume_router_uses_fallback(self):
        support = self.root / "support"
        support.mkdir()
        now = 9_000.0
        (support / maintenance_status.MONITOR_STATE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "health": {
                        "last_sample_at": now - 5,
                        "sample_interval_seconds": 10,
                        "last_error": "",
                        "last_prompt_error": "",
                    },
                    "return_health": {
                        "last_success_at": now - 4,
                        "fallback": True,
                    },
                    "incidents": [],
                    "active": {},
                    "pending_prompts": [],
                }
            )
        )

        status = maintenance_status.resource_monitor_status(
            support,
            {"healthy": True, "state": "running"},
            now,
        )

        self.assertEqual(status["state"], "degraded")
        self.assertFalse(status["healthy"])
        self.assertTrue(status["last_return_fallback"])
        self.assertEqual(status["last_return_success_at"], now - 4)


if __name__ == "__main__":
    unittest.main()
