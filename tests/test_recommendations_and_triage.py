"""Unit tests for routine-process recurrence gating and recommendations digest."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from process_triage import triage_process
import recommendations


class ProcessTriageRecurrenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = {
            "name": "mds_stores",
            "cpu_samples": [25.0, 28.0],
            "io_samples": [5.0, 6.0],
            "peak_memory_mb": 400.0,
        }
        self.guidance = {
            "category": "spotlight",
            "expected_cpu": 40.0,
            "expected_memory_mb": 1000.0,
            "expected_total_mib_s": 20.0,
            "expected_write_mib_s": 10.0,
        }

    def test_default_config_preserves_recurrence_review(self) -> None:
        result = triage_process(self.proc, self.guidance, {}, recurrence=True)
        self.assertEqual(result["decision"], "review")

    def test_threshold_suppresses_below_limit_unless_ceiling_exceeded(self) -> None:
        cfg = {
            "process_routine_suppression_enabled": True,
            "process_routine_recurrence_threshold": 3,
            "process_routine_review_multiplier": 4.0,
        }
        first = triage_process(self.proc, self.guidance, cfg, recurrence=True, recurrence_count=1)
        second = triage_process(self.proc, self.guidance, cfg, recurrence=True, recurrence_count=2)
        third = triage_process(self.proc, self.guidance, cfg, recurrence=True, recurrence_count=3)
        self.assertEqual(first["decision"], "suppress")
        self.assertEqual(second["decision"], "suppress")
        self.assertEqual(third["decision"], "review")

        spike_proc = {
            "name": "mds_stores",
            "cpu_samples": [310.0, 320.0],
            "io_samples": [5.0, 6.0],
            "peak_memory_mb": 400.0,
        }
        spike = triage_process(spike_proc, self.guidance, cfg, recurrence=True, recurrence_count=1)
        self.assertEqual(spike["decision"], "review")


class RecommendationsTests(unittest.TestCase):
    def test_build_cooldown_and_sync_privacy(self) -> None:
        fake_home = Path("/Users/synthetic")
        audit = {
            "launchagents": {
                "failing_loops": [
                    {"label": "com.example.worker", "state": "failed", "schedule": "60s"},
                ]
            },
            "oversized_logs": [
                {"path": "/Users/synthetic/Library/Logs/big.log", "mb": 42.5},
                {"path": "/Users/synthetic/Library/Logs/small.log", "mb": 4.0},
            ],
            "swap": {"swap_used_mb": 2048},
            "processes": {"top_rss": [{"name": "HelperApp", "rss_mb": 1200}]},
        }
        recs = recommendations.build_recommendations(
            audit,
            stale_apps=["/Applications/UnusedApp.app"],
            home=fake_home,
        )
        keys = {r["key"] for r in recs}
        self.assertEqual(
            keys,
            {
                "launchagent-loop:com.example.worker",
                "log-bloat:~/Library/Logs/big.log",
                "swap-pressure:high",
                "stale-apps:1",
            },
        )

        fresh1, state1 = recommendations.filter_active_recommendations(recs, {}, now=1000.0)
        self.assertEqual(len(fresh1), 4)
        fresh2, _ = recommendations.filter_active_recommendations(recs, state1, now=1000.0 + 3600.0)
        self.assertEqual(fresh2, [])
        fresh3, _ = recommendations.filter_active_recommendations(
            recs, state1, now=1000.0 + recommendations.DEFAULT_COOLDOWN_SECONDS + 1.0
        )
        self.assertEqual(len(fresh3), 4)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            audit_path = tmp_path / "audit.json"
            rec_state = tmp_path / "rec_state.json"
            out_path = tmp_path / "idle-recommendations.md"
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

            summary = recommendations.sync_recommendations(
                audit_state_path=audit_path,
                rec_state_path=rec_state,
                out_path=out_path,
                stale_apps=["/Applications/UnusedApp.app"],
                now=5000.0,
            )
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["newly_surfaced"], 4)
            md = out_path.read_text(encoding="utf-8")
            self.assertNotIn(str(Path.home()), md)
            self.assertIn("com.example.worker", md)
            self.assertIn("42.5 MiB", md)
