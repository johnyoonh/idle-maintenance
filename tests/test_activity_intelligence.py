import json
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from activity_intelligence import (
    ActivityWatchContext,
    DiagnosisResult,
    OpenAIAPI,
    VectorEventStore,
    app_usage_context,
    cosine_similarity,
    embed_text,
    incident_event,
    install_codex_event_hook,
    main,
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

    def test_cli_help_lists_canonical_commands(self):
        output = io.StringIO()
        with self.assertRaisesRegex(SystemExit, "0"), redirect_stdout(output):
            main(["--help"])
        self.assertIn("process", output.getvalue())
        self.assertIn("status", output.getvalue())
        self.assertIn("record", output.getvalue())

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

    def test_two_spikes_trigger_when_latest_is_materially_worse(self):
        diagnoser = FakeDiagnoser()
        first = self.incident(1, peak=40)
        second = self.incident(2, peak=70)
        second["started_at"] = self.clock
        self.write_state([first, second])
        result = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=diagnoser,
            notify_fn=lambda *_args: None,
            now_fn=lambda: self.clock,
        )
        self.assertEqual(result["diagnoses"], 1)

    def test_low_confidence_structured_diagnosis_is_saved_without_notification(self):
        class LowConfidenceDiagnoser(FakeDiagnoser):
            def diagnose(self, prompt):
                self.prompts.append(prompt)
                diagnosis = {
                    "summary": "Pattern is uncertain.",
                    "likely_causes": [],
                    "remedies": ["Observe one more recurrence"],
                    "evidence": ["Three similar spikes"],
                    "verification": "Compare the next sample.",
                    "confidence": 0.4,
                    "urgency": "low",
                    "uncertainty": "Insufficient context.",
                }
                return DiagnosisResult(True, "Pattern is uncertain.", diagnosis=diagnosis)

        notifications = []
        self.write_state([self.incident(1), self.incident(2), self.incident(3)])
        result = run_cycle(
            self.config,
            db_path=self.db,
            resource_state_path=self.state,
            app_usage_path=self.usage,
            report_path=self.report,
            diagnoser=LowConfidenceDiagnoser(),
            notify_fn=lambda *args: notifications.append(args),
            now_fn=lambda: self.clock,
        )
        self.assertEqual(result["diagnoses"], 1)
        self.assertEqual(notifications, [])

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

    def test_codex_hook_records_metadata_without_prompt_text(self):
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
        self.assertEqual(rows[0]["summary"], "codex investigation | project tmp")
        self.assertNotIn("fileproviderd", rows[0]["summary"])

    def test_codex_hook_preserves_launcher_options(self):
        calls = []

        def launch(prompt, cwd="/", **kwargs):
            calls.append((prompt, cwd, kwargs))
            return True, "iTerm", True

        core = SimpleNamespace(open_codex_in_terminal=launch)
        install_codex_event_hook(core, db_path=self.db)
        runner = object()

        result = core.open_codex_in_terminal(
            "synthetic prompt",
            "/tmp",
            launch_runner=runner,
        )

        self.assertEqual(result, (True, "iTerm", True))
        self.assertIs(calls[0][2]["launch_runner"], runner)

    def test_openai_diagnoser_uses_tool_free_structured_nonstored_response(self):
        seen = []

        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return self.payload

        diagnosis = {
            "summary": "Repeated cloud sync churn.",
            "likely_causes": ["A recurring sync backlog"],
            "remedies": ["Pause the triggering bulk change"],
            "evidence": ["Three similar incidents"],
            "verification": "Confirm disk throughput returns to baseline.",
            "confidence": 0.85,
            "urgency": "medium",
            "uncertainty": "Per-process counters do not prove physical disk attribution.",
        }

        def opener(request, timeout):
            seen.append((request, timeout))
            return Response({"output": [{"content": [{"type": "output_text", "text": json.dumps(diagnosis)}]}]})

        engine = OpenAIAPI(self.config, api_key="synthetic-key", opener=opener)
        result = engine.diagnose("diagnose repeated pattern")
        self.assertTrue(result.ok)
        payload = json.loads(seen[0][0].data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("tools", payload)
        self.assertEqual(result.diagnosis["confidence"], 0.85)

    def test_openai_embedding_uses_sanitized_text_and_configured_dimensions(self):
        seen = []

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return json.dumps({"data": [{"embedding": [0.0] * 512}]}).encode()

        def opener(request, timeout):
            seen.append(json.loads(request.data))
            return Response()

        vector = OpenAIAPI(self.config, api_key="synthetic-key", opener=opener).embed(
            "resource spike | foreground Codex | codex-projects idle-maintenance"
        )
        self.assertEqual(len(vector), 512)
        self.assertEqual(seen[0]["model"], "text-embedding-3-small")
        self.assertEqual(seen[0]["dimensions"], 512)
        self.assertNotIn("prompt", seen[0]["input"].lower())

    def test_activitywatch_context_discards_titles_and_keeps_coarse_fields(self):
        responses = {
            "/api/0/buckets": {
                "window": {"type": "currentwindow"},
                "metrics": {"type": "os.performance.sample"},
            },
            "/api/0/buckets/window/events": [
                {"timestamp": "2027-01-15T08:00:00Z", "data": {"app": "Codex", "title": "private client prompt"}}
            ],
            "/api/0/buckets/metrics/events": [
                {"timestamp": "2027-01-15T08:00:00Z", "data": {"cpu_idle_percent": 12, "load_1m": 8, "secret": "drop"}}
            ],
        }

        class Response:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return json.dumps(self.value).encode()

        def opener(request, timeout):
            path = request.full_url.split("http://aw.local", 1)[1].split("?", 1)[0]
            return Response(responses[path])

        context = ActivityWatchContext("http://aw.local", opener=opener).context_at(1_800_000_000, 1200)
        encoded = json.dumps(context)
        self.assertEqual(context["foreground_apps"], ["Codex"])
        self.assertEqual(context["performance"], {"cpu_idle_percent": 12.0, "load_1m": 8.0})
        self.assertNotIn("private client prompt", encoded)
        self.assertNotIn("secret", encoded)

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

    def test_store_uses_sqlite_vec_when_dependency_is_installed(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self.skipTest("sqlite-vec is not installed in this interpreter")
        with VectorEventStore(self.db) as store:
            self.assertEqual(store.vector_backend, "sqlite-vec")

    def test_store_migrates_legacy_vector_dimensions(self):
        connection = sqlite3.connect(self.db)
        connection.executescript("""
            CREATE TABLE events(
              event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, source TEXT NOT NULL,
              kind TEXT NOT NULL, summary TEXT NOT NULL, vector_json TEXT NOT NULL,
              payload_json TEXT NOT NULL, reviewed_at REAL);
            CREATE TABLE diagnoses(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              trigger_event_id TEXT NOT NULL, centroid_json TEXT NOT NULL,
              event_ids_json TEXT NOT NULL, response TEXT NOT NULL, model TEXT NOT NULL);
        """)
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,NULL)",
            ("legacy", self.clock, "idle-maintenance", "resource-spike", "resource spike cloud sync", json.dumps([0.0] * 96), "{}"),
        )
        connection.commit()
        connection.close()
        with VectorEventStore(self.db) as store:
            row = store.connection.execute("SELECT vector_json FROM events WHERE event_id='legacy'").fetchone()
            if store.vector_backend == "sqlite-vec":
                self.assertEqual(len(json.loads(row[0])), 512)
            columns = {value[1] for value in store.connection.execute("PRAGMA table_info(diagnoses)")}
            self.assertIn("diagnosis_json", columns)


if __name__ == "__main__":
    unittest.main()
