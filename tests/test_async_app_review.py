from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import maintenance_interactive as maintenance


class AsyncAppReviewTests(unittest.TestCase):
    def persisted_paths(self, root: Path):
        return root / "queue.json", root / "whitelist.json"

    def test_keep_is_persisted_before_review_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path, whitelist_path = self.persisted_paths(Path(tmp))
            queue = [{"path": "/Applications/Demo.app", "last_prompted": 0}]
            whitelist = {}
            with patch.object(maintenance._core, "QUEUE_PATH", str(queue_path)), patch.object(
                maintenance._core, "WHITELIST_PATH", str(whitelist_path)
            ):
                updated, done, delta = maintenance._handle_app_action(
                    "KEEP", queue[0], list(queue), whitelist, {}
                )
            self.assertTrue(done)
            self.assertEqual(delta, 1)
            self.assertEqual(updated, [])
            self.assertEqual(json.loads(queue_path.read_text(encoding="utf-8")), [])
            persisted_whitelist = json.loads(whitelist_path.read_text(encoding="utf-8"))
            self.assertIn("/Applications/Demo.app", persisted_whitelist)

    def test_snooze_is_persisted_before_review_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path, whitelist_path = self.persisted_paths(Path(tmp))
            queue = [{"path": "/Applications/Demo.app", "last_prompted": 0}]
            whitelist = {}
            with patch.object(maintenance._core, "QUEUE_PATH", str(queue_path)), patch.object(
                maintenance._core, "WHITELIST_PATH", str(whitelist_path)
            ):
                updated, done, delta = maintenance._handle_app_action(
                    "SNOOZE", queue[0], list(queue), whitelist, {}, now_fn=lambda: 1234
                )
            self.assertTrue(done)
            self.assertEqual(delta, 1)
            self.assertEqual(updated[0]["last_prompted"], 1234)
            persisted = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[0]["last_prompted"], 1234)

    def test_confirmed_trash_is_durably_queued_before_worker_launch_and_advance(self):
        queue = [{"path": "/Applications/Demo.app", "last_prompted": 0}]
        events = []

        def enqueue(path):
            events.append(("enqueue", path))
            return {"id": "job", "state": "pending"}

        def persist(current, whitelist):
            events.append(("persist", list(current)))

        def launch(**kwargs):
            events.append(("launch", kwargs.get("base_dir")))
            return True

        with patch.object(maintenance, "_persist_app_state", side_effect=persist):
            updated, done, delta = maintenance._handle_app_action(
                "DELETE",
                queue[0],
                list(queue),
                {},
                {},
                enqueue_fn=enqueue,
                launch_fn=launch,
            )
        self.assertEqual([event[0] for event in events], ["enqueue", "persist", "launch"])
        self.assertEqual(updated, [])
        self.assertTrue(done)
        self.assertEqual(delta, 1)

    def test_trash_queue_failure_keeps_current_review_active(self):
        queue = [{"path": "/Applications/Demo.app", "last_prompted": 0}]
        launch = Mock()
        with patch.object(maintenance._core, "log"), patch.object(
            maintenance._core, "notify_user"
        ) as notify:
            updated, done, delta = maintenance._handle_app_action(
                "DELETE",
                queue[0],
                list(queue),
                {},
                {},
                enqueue_fn=Mock(side_effect=OSError("disk full")),
                launch_fn=launch,
            )
        self.assertEqual(updated, queue)
        self.assertFalse(done)
        self.assertEqual(delta, 0)
        launch.assert_not_called()
        self.assertIn("review remains open", notify.call_args.args[1])

    def test_open_compatibility_path_never_advances_or_updates_snooze_state(self):
        queue = [{"path": "/Applications/Demo.app", "last_prompted": 0}]
        runner = Mock()
        updated, done, delta = maintenance._handle_app_action(
            "TRY", queue[0], list(queue), {}, {}, open_runner=runner
        )
        self.assertEqual(updated, queue)
        self.assertFalse(done)
        self.assertEqual(delta, 0)
        self.assertEqual(updated[0]["last_prompted"], 0)
        runner.assert_called_once_with(["open", "/Applications/Demo.app"], check=False)

    def test_process_review_entrypoint_is_not_replaced(self):
        source = Path(maintenance.__file__).read_text(encoding="utf-8")
        self.assertIn('if len(sys.argv) > 1 and sys.argv[1] == "--process-audit":\n            _result = _core.main()', source)

    def test_app_audit_snapshot_is_reused_and_finishes_before_first_prompt(self):
        events = []
        audit = Mock()
        audit.return_value.returncode = 0
        audit.return_value.stdout = "/Applications/Demo.app|2025-01-01\n"

        def run_audit(*_args, **_kwargs):
            events.append("audit")
            return audit.return_value

        def process_audit(_config, **kwargs):
            events.append("process")
            self.assertEqual(
                kwargs["planned_app_output"],
                ["/Applications/Demo.app|2025-01-01"],
            )
            return True, 0

        def prompt(*_args, **_kwargs):
            events.append("prompt")
            return "KEEP"

        config = {"max_prompts": 1, "max_entries_per_idle_return": 1}
        with (
            patch.object(maintenance, "_interactive_lock", return_value=contextlib.nullcontext(True)),
            patch.object(maintenance, "load_config", return_value=config),
            patch.object(maintenance.subprocess, "run", side_effect=run_audit) as runner,
            patch.object(maintenance._app_actions, "launch_worker", return_value=True),
            patch.object(maintenance._app_actions, "active_action_paths", return_value=set()),
            patch.object(maintenance._core, "run_process_audit", side_effect=process_audit),
            patch.object(maintenance._core, "load_json", return_value=[]),
            patch.object(maintenance._core, "load_custom_whitelist", return_value={}),
            patch.object(maintenance._core, "queue_item_is_snoozed", return_value=False),
            patch.object(maintenance._core, "app_usage_detail", return_value="detail"),
            patch.object(
                maintenance._core,
                "get_restore_source",
                return_value={"source": "brew", "restore_command": "brew install --cask demo"},
            ),
            patch.object(maintenance._core, "app_cleanup_config", return_value=({}, None)),
            patch.object(maintenance._core, "prompt_user", side_effect=prompt),
            patch.object(maintenance._core, "record_keep", side_effect=lambda state, path: state.update({path: 1})),
            patch.object(maintenance._core, "save_json", return_value=True),
        ):
            maintenance._run_app_review()

        runner.assert_called_once()
        self.assertEqual(events, ["audit", "process", "prompt"])


if __name__ == "__main__":
    unittest.main()
