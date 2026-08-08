import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import prompt_session
import review_ui


class FakeProcess:
    def __init__(self, responses):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(f"{value}\n" for value in responses))
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class PromptSessionTests(unittest.TestCase):
    def test_session_reuses_one_swift_process(self):
        fake = FakeProcess(["KEEP", "SNOOZE"])
        runner = Mock(return_value=fake)
        session = prompt_session.PromptSession("/tmp/repo", runner=runner)

        first = session.ask({"name": "one", "path": "/one"})
        second = session.ask({"name": "two", "path": "/two"})

        self.assertEqual(first, "KEEP")
        self.assertEqual(second, "SNOOZE")
        runner.assert_called_once()
        requests = [json.loads(line) for line in fake.stdin.getvalue().splitlines()]
        self.assertEqual([item["name"] for item in requests], ["one", "two"])

    def test_close_is_safe_after_session_failure(self):
        fake = FakeProcess([])
        runner = Mock(return_value=fake)
        session = prompt_session.PromptSession("/tmp/repo", runner=runner)
        with self.assertRaises(RuntimeError):
            session.ask({"name": "one", "path": "/one"})
        self.assertIsNone(session.process)

    def test_launch_failure_falls_back_to_legacy_prompt(self):
        with (
            patch("prompt_session.get_session") as get_session,
            patch("prompt_session.legacy_prompt", return_value="KEEP") as legacy,
        ):
            get_session.return_value.ask.side_effect = OSError("swift unavailable")
            result = prompt_session.ask_review("/repo", {"name": "one", "path": "/one"})

        self.assertEqual(result, "KEEP")
        legacy.assert_called_once()


class ReviewUiTests(unittest.TestCase):
    def test_io_trigger_does_not_present_zero_cpu_as_the_reason(self):
        proc = {
            "cpu": 0,
            "cpu_samples": [0.0],
            "io_samples": [{"total_mib_s": 28.0, "write_mib_s": 4.0}],
            "reason": "I/O charged to the process averaged 28.0 MiB/s",
        }
        headline = review_ui.process_headline(proc)
        self.assertEqual(headline, "I/O trigger: peak 28.0 MiB/s • CPU sample 0.0%")

    def test_process_copy_payload_matches_investigate_prompt(self):
        proc = {
            "pid": 42,
            "cpu": 0,
            "cpu_samples": [0.0],
            "io_samples": [{"total_mib_s": 28.0, "write_mib_s": 4.0}],
            "etime": "00:10:00",
            "reason": "high I/O",
            "command": "/usr/libexec/example",
            "comm": "example",
        }
        core = SimpleNamespace(BASE_DIR="/repo")
        pr = SimpleNamespace(
            ATTRIBUTION_NOTE="attribution note",
            _display=lambda value: "example (/usr/libexec/example)",
            _known_context=lambda value: [],
            process_action_policy=lambda value: "protected",
            investigation_prompt=lambda core_value, value, config: "exact investigate prompt",
        )
        with patch("review_ui.ask_review", return_value="KEEP") as ask:
            result = review_ui.prompt_process(
                core,
                pr,
                proc,
                config={"x": 1},
                pending=3,
            )

        self.assertEqual(result, "KEEP")
        payload = ask.call_args.args[1]
        self.assertEqual(payload["copyText"], "exact investigate prompt")
        self.assertEqual(payload["pending"], 3)
        self.assertTrue(payload["headline"].startswith("I/O trigger:"))

    def test_app_pending_count_uses_live_queue_state(self):
        core = SimpleNamespace(
            queue_item_is_snoozed=lambda item, hours: bool(item.get("snoozed"))
        )

        def active_main_frame():
            current_queue = [
                {"path": "/one", "snoozed": False},
                {"path": "/two", "snoozed": True},
                {"path": "/three", "snoozed": False},
            ]
            processed = 1
            max_prompts = 5
            app_snooze_hours = 24
            self.assertEqual(len(current_queue), 3)
            self.assertEqual(processed, 1)
            self.assertEqual(max_prompts, 5)
            self.assertEqual(app_snooze_hours, 24)
            return review_ui._pending_app_reviews(core)

        self.assertEqual(active_main_frame(), 2)

    def test_planned_app_count_is_included_in_run_budget(self):
        core = SimpleNamespace(
            BASE_DIR="/repo",
            DEFAULT_MAX_PROMPTS=5,
            QUEUE_PATH="/queue.json",
            WHITELIST_PATH="/whitelist.json",
            load_json=lambda path: [],
            load_custom_whitelist=lambda path: {},
            queue_item_is_snoozed=lambda item, hours: item.get("path") == "/two.app",
        )
        config = {"max_prompts": 5, "app_snooze_hours": 720}
        output = "/one.app|2026-01-01\n/two.app|2026-01-01\n/three.app|2026-01-01\n"
        with patch("review_ui.subprocess.check_output", return_value=output):
            pending = review_ui._planned_app_reviews(core, config, process_pending=2, prompt_budget=5)

        self.assertEqual(pending, 2)


if __name__ == "__main__":
    unittest.main()
