import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from activity_intelligence import (
    CodexDiagnoser,
    DiagnosisResult,
    VectorEventStore,
    app_usage_context,
    cosine_similarity,
    embed_text,
    incident_event,
    install_codex_event_hook,
    record_external_event,
    run_cycle,
    status,
)


class FakeDiagnoser:
    def __init__(self):
        self.prompts = []

    def diagnose(self, prompt):
        self.prompts.append(prompt)
        return DiagnosisResult(True, "Likely repeated cloud sync churn. Pause the triggering bulk change, then verify sync backlog falls.")


class ActivityIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "events.sqlite3"
        self.state = self.root / "resource-state.json"
        self.usage = self.root / "app_usage.json"
        self.report = self.root / "latest.md"
        self.clock = 1_800_000_000.0
        self.config = {
            "activity_intelligence_enabled": True,
            "activity_intelligence_min_pattern_events": 3,
            "activity_intelligence_similarity_threshold": 0.70,
            "activity_intelligence_lookback_days": 7,
            "activity_intelligence_retention_days": 30,
            "activity_intelligence_max_events": 100,
            "activity_intelligence_diagnosis_cooldown_hours": 24,
            "activity_intelligence_diagnosis_similarity_threshold": 0.82,
            "activity_intelligence_max_diagnoses_per_cycle": 1,
            "activity_intelligence_context_minutes": 20,
        }

    def tearDown(self):
        self.temp.cleanup()

    def incident(self, index, *, process="fileproviderd", group="cloud-sync", peak=45.0):
        timestamp = self.clock - (3 - index) * 600
        return {
            "id": f"incident-{index}",
            "process": process,
            "process_key": f"process:{process}",
            "started_at": timestamp,
            "status": "recovered",
            "recurrence": index > 1,
            "peak_total_mib_s": peak,
            "peak_write_mib_s": peak / 2,
            "system_mib_s": 80,
            "known_process": {
                "recurrence_group": group,
                "role": "iCloud/File Provider synchronization",
                "default_action": "Inspect the sync backlog before terminating anything.",
            },
            "triage": {
                "classification": "known-routine",
                "decision": "review",
                "reason": "repeated high I/O",
            },
        }

    def write_state(self, incidents):
        self.state.write_text(json.dumps({"schema_version": 1, "incidents": incidents}))

    def test_hash_vectors_prefer_same_pattern(self):
        first = embed_text("resource spike fileproviderd group cloud-sync foreground Finder")
        same = embed_text("resource spike fileproviderd group cloud-sync recurrent foreground Finder")
        other = embed_text("resource spike photoanalysisd group photos-analysis foreground Photos")
        self.assertGreater(cosine_similarity(first, same), cosine_similarity(first, other))

    def test_app_usage_context_removes_paths(self):
        self.usage.write_text(json.dumps({
            "/Applications/Finder.app": self.clock - 30,
            "/Applications/Obsidian.app": self.clock - 60,
            "/Applications/Old.app": self.clock - 10_000,
        }))
        self.assertEqual(
            app_usage_context(self.usage, self.clock, 120),
            ["Finder", "Obsidian"],
        )

    def test_incident_event_contains_bounded_activity_context(self):
        self.usage.write_text(json.dumps({"/Applications/Obsidian.app": self.clock - 600}))
        event = incident_event(self.incident(3), app_usage_path=self.usage, context_minutes=20)
        self.assertEqual(event["source"], "idle-maintenance")
        self.assertEqual(event["kind"], "resource-spike")
        self.assertIn("foreground Obsidian", event["summary"])
        self.assertNotIn("/Applications/", json.dumps(event))

    def test_single_and_double_spikes_do_not_call_llm(self):
        diagnoser = FakeDiagnoser()
        notifications = []
        self.write_state([self.incident(1), self.incident(2)])
        result = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=diagnoser,
            notify_fn=lambda title, message: notifications.append((title, message)),
            now_fn=lambda: self.clock,
        )
        self.assertEqual(result["diagnoses"], 0)
        self.assertEqual(diagnoser.prompts, [])
        self.assertEqual(notifications, [])

    def test_three_similar_spikes_trigger_one_smart_diagnosis(self):
        diagnoser = FakeDiagnoser()
        notifications = []
        self.usage.write_text(json.dumps({"/Applications/Finder.app": self.clock - 30}))
        self.write_state([self.incident(1), self.incident(2), self.incident(3)])
        result = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=diagnoser,
            notify_fn=lambda title, message: notifications.append((title, message)),
            now_fn=lambda: self.clock,
        )
        self.assertEqual(result["diagnoses"], 1)
        self.assertEqual(len(diagnoser.prompts), 1)
        self.assertIn("Pattern contains 3", diagnoser.prompts[0])
        self.assertIn("Nearby activity context", diagnoser.prompts[0])
        self.assertEqual(len(notifications), 1)
        self.assertTrue(self.report.exists())
        latest = status(self.db)["latest"]
        self.assertEqual(len(latest["event_ids"]), 3)

    def test_recent_similar_diagnosis_prevents_repeat(self):
        first_diagnoser = FakeDiagnoser()
        self.write_state([self.incident(1), self.incident(2), self.incident(3)])
        first = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=first_diagnoser,
            notify_fn=lambda *_args: None,
            now_fn=lambda: self.clock,
        )
        self.assertEqual(first["diagnoses"], 1)

        incidents = [self.incident(1), self.incident(2), self.incident(3), self.incident(4)]
        incidents[-1]["started_at"] = self.clock + 60
        self.write_state(incidents)
        second_diagnoser = FakeDiagnoser()
        second = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=second_diagnoser,
            notify_fn=lambda *_args: None,
            now_fn=lambda: self.clock + 60,
        )
        self.assertEqual(second["diagnoses"], 0)
        self.assertEqual(second_diagnoser.prompts, [])

    def test_external_events_share_same_local_store(self):
        event_id = record_external_event(
            source="codex",
            kind="investigation",
            summary="codex investigation | fileproviderd cloud-sync",
            db_path=self.db,
            timestamp=self.clock,
        )
        self.assertTrue(event_id.startswith("codex:"))
        counts = status(self.db)
        self.assertEqual(counts["events"], 1)

    def test_codex_hook_records_idle_maintenance_investigation(self):
        calls = []
        core = SimpleNamespace(
            open_codex_in_terminal=lambda prompt, cwd="/": calls.append((prompt, cwd)) or (True, "Terminal", True)
        )
        install_codex_event_hook(core, db_path=self.db)
        core.open_codex_in_terminal("- Command: fileproviderd\n- Reason: repeated sync churn", "/tmp")
        self.assertEqual(len(calls), 1)
        with VectorEventStore(self.db) as store:
            rows = store.connection.execute("SELECT source, kind, summary FROM events").fetchall()
        self.assertEqual(rows[0]["source"], "codex")
        self.assertEqual(rows[0]["kind"], "investigation")
        self.assertIn("fileproviderd", rows[0]["summary"])

    def test_codex_diagnoser_uses_ephemeral_read_only_command_and_prompt_argument(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="A bounded remedy", stderr="")

        engine = CodexDiagnoser(self.config, command_runner=runner)
        result = engine.diagnose("diagnose repeated pattern")
        self.assertTrue(result.ok)
        self.assertEqual(seen["command"][-1], "diagnose repeated pattern")
        self.assertIn("--ephemeral", seen["command"])
        self.assertIn("--ignore-user-config", seen["command"])
        self.assertIn("read-only", seen["command"])
        self.assertNotIn("input", seen["kwargs"])
        self.assertEqual(seen["kwargs"]["cwd"], "/")

    def test_store_deduplicates_event_ids(self):
        event = {
            "event_id": "same",
            "timestamp": self.clock,
            "source": "activity-watcher",
            "kind": "foreground-activity",
            "summary": "foreground app Obsidian",
            "payload": {},
        }
        with VectorEventStore(self.db) as store:
            self.assertTrue(store.add_event(event))
            self.assertFalse(store.add_event(event))
            self.assertEqual(store.counts()["events"], 1)


if __name__ == "__main__":
    unittest.main()
