from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app_actions
import maintenance_core


class AppActionWorkerTests(unittest.TestCase):
    def paths(self, root: Path):
        return (
            str(root / "app-actions.json"),
            str(root / "state.lock"),
            str(root / "worker.lock"),
        )

    def test_enqueue_is_durable_pending_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, _ = self.paths(Path(tmp))
            job = app_actions.enqueue_trash_action(
                "/Applications/Demo.app",
                state_path=state_path,
                lock_path=state_lock,
                now=100,
                job_id="job-1",
            )
            on_disk = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertEqual(job["state"], "pending")
            self.assertEqual(on_disk["jobs"][0]["id"], "job-1")
            self.assertEqual(on_disk["jobs"][0]["state"], "pending")

    def test_worker_executes_jobs_strictly_serially(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            app_actions.enqueue_trash_action(
                "/Applications/A.app", state_path=state_path, lock_path=state_lock, now=1, job_id="a"
            )
            app_actions.enqueue_trash_action(
                "/Applications/B.app", state_path=state_path, lock_path=state_lock, now=2, job_id="b"
            )
            observed = []

            def execute(job):
                observed.append((job["id"], job["state"]))
                snapshot = app_actions._load_state_unlocked(state_path)
                running = [entry for entry in snapshot["jobs"] if entry.get("state") == "running"]
                self.assertEqual(len(running), 1)
                return {"state": "completed", "result": {"outcome": "trashed"}}

            self.assertEqual(
                app_actions.run_worker(
                    state_path=state_path,
                    state_lock_path=state_lock,
                    worker_lock_path=worker_lock,
                    execute=execute,
                    now_fn=lambda: 100,
                ),
                0,
            )
            self.assertEqual(observed, [("a", "running"), ("b", "running")])
            states = {entry["id"]: entry["state"] for entry in app_actions._load_state_unlocked(state_path)["jobs"]}
            self.assertEqual(states, {"a": "completed", "b": "completed"})

    def test_singleton_worker_lock_prevents_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            app_actions.enqueue_trash_action(
                "/Applications/A.app", state_path=state_path, lock_path=state_lock, now=1
            )
            seen = Mock()
            with open(worker_lock, "a+", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(
                    app_actions.run_worker(
                        state_path=state_path,
                        state_lock_path=state_lock,
                        worker_lock_path=worker_lock,
                        execute=seen,
                        now_fn=lambda: 100,
                    ),
                    0,
                )
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            seen.assert_not_called()
            self.assertEqual(app_actions.app_action_status(state_path=state_path, lock_path=state_lock, now=100)["queued"], 1)

    def test_pending_jobs_resume_but_interrupted_running_job_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            Path(state_path).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "jobs": [
                            {
                                "id": "old-running",
                                "action": "trash",
                                "app_path": "/Applications/Old.app",
                                "state": "running",
                                "requested_at": 1,
                                "started_at": 2,
                                "finished_at": None,
                                "error": "",
                                "result": {},
                            },
                            {
                                "id": "pending",
                                "action": "trash",
                                "app_path": "/Applications/New.app",
                                "state": "pending",
                                "requested_at": 3,
                                "started_at": None,
                                "finished_at": None,
                                "error": "",
                                "result": {},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            seen = []
            app_actions.run_worker(
                state_path=state_path,
                state_lock_path=state_lock,
                worker_lock_path=worker_lock,
                execute=lambda job: seen.append(job["id"]) or {"state": "completed", "result": {"outcome": "trashed"}},
                now_fn=lambda: 100,
            )
            jobs = {entry["id"]: entry for entry in app_actions._load_state_unlocked(state_path)["jobs"]}
            self.assertEqual(seen, ["pending"])
            self.assertEqual(jobs["old-running"]["state"], "failed")
            self.assertIn("not retried", jobs["old-running"]["error"])
            self.assertEqual(jobs["pending"]["state"], "completed")

    def test_failed_job_is_retained_and_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            app_actions.enqueue_trash_action(
                "/Applications/A.app", state_path=state_path, lock_path=state_lock, now=1, job_id="failed"
            )
            execute = Mock(return_value={"state": "failed", "error": "permission denied", "result": {"outcome": "delete-failed"}})
            for _ in range(2):
                app_actions.run_worker(
                    state_path=state_path,
                    state_lock_path=state_lock,
                    worker_lock_path=worker_lock,
                    execute=execute,
                    now_fn=lambda: 100,
                )
            self.assertEqual(execute.call_count, 1)
            status = app_actions.app_action_status(state_path=state_path, lock_path=state_lock, now=100)
            self.assertEqual(status["failed"], 1)
            self.assertEqual(status["most_recent_failure"]["error"], "permission denied")

    def test_missing_app_is_terminal_success_and_notified(self):
        fake_core = types.ModuleType("maintenance_core")
        fake_core.notify_user = Mock()
        fake_core.log = Mock()
        with patch.dict(sys.modules, {"maintenance_core": fake_core}):
            outcome = app_actions._execute_trash_job({"app_path": "/Applications/DefinitelyMissing.app"})
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["result"]["outcome"], "missing-app")
        fake_core.notify_user.assert_called_once()

    def test_success_reuses_core_delete_and_notifies_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Demo.app"
            app.mkdir()
            fake_core = types.ModuleType("maintenance_core")
            fake_core.notify_user = Mock()
            fake_core.log = Mock()
            fake_core.app_cleanup_config = Mock(return_value=({"delete_mode": "trash"}, {"before_delete_app": []}))
            fake_core.get_restore_source = Mock(
                return_value={"source": "homebrew", "restore_command": "brew install --cask demo"}
            )
            fake_core.delete_app = Mock(return_value=True)
            with patch.dict(sys.modules, {"maintenance_core": fake_core}), patch.object(
                app_actions, "load_config", return_value={"app_cleanup": {}}
            ):
                outcome = app_actions._execute_trash_job({"app_path": str(app)}, base_dir=str(Path(tmp)))
            self.assertEqual(outcome["state"], "completed")
            self.assertEqual(outcome["result"]["outcome"], "trashed")
            fake_core.delete_app.assert_called_once()
            self.assertIn("Restore with", fake_core.notify_user.call_args.args[1])

    def test_terminal_retention_keeps_active_and_latest_100_for_30_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, _ = self.paths(Path(tmp))
            now = 40 * 24 * 60 * 60
            jobs = [
                {
                    "id": "pending",
                    "action": "trash",
                    "app_path": "/Applications/Pending.app",
                    "state": "pending",
                    "requested_at": 1,
                    "started_at": None,
                    "finished_at": None,
                    "error": "",
                    "result": {},
                }
            ]
            for index in range(105):
                jobs.append(
                    {
                        "id": f"recent-{index}",
                        "action": "trash",
                        "app_path": f"/Applications/{index}.app",
                        "state": "completed",
                        "requested_at": index,
                        "started_at": index,
                        "finished_at": now - index,
                        "error": "",
                        "result": {"outcome": "trashed"},
                    }
                )
            jobs.append(
                {
                    "id": "expired",
                    "action": "trash",
                    "app_path": "/Applications/Expired.app",
                    "state": "failed",
                    "requested_at": 1,
                    "started_at": 1,
                    "finished_at": now - app_actions.TERMINAL_RETENTION_SECONDS - 1,
                    "error": "old",
                    "result": {},
                }
            )
            Path(state_path).write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")
            app_actions.app_action_status(state_path=state_path, lock_path=state_lock, now=now)
            retained = app_actions._load_state_unlocked(state_path)["jobs"]
            self.assertEqual(sum(entry["state"] in app_actions.TERMINAL_STATES for entry in retained), 100)
            self.assertTrue(any(entry["id"] == "pending" for entry in retained))
            self.assertFalse(any(entry["id"] == "expired" for entry in retained))


class DeleteCompatibilityTests(unittest.TestCase):
    def config(self, ledger: str) -> dict:
        return {
            "app_cleanup": {
                "allow_unknown_restore_source": True,
                "delete_mode": "trash",
                "deletion_ledger": ledger,
            },
            "hooks": {"before_delete_app": ["/tmp/hook"], "after_delete_app": []},
        }

    def test_hook_veto_refuses_delete_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Demo.app"
            app.mkdir()
            with patch.object(
                maintenance_core,
                "get_restore_source",
                return_value={"source": "homebrew", "restore_command": "brew install --cask demo"},
            ), patch.object(maintenance_core, "app_metadata", return_value={}), patch.object(
                maintenance_core, "run_delete_hooks", return_value=False
            ), patch.object(maintenance_core, "notify_user") as notify:
                self.assertFalse(maintenance_core.delete_app(str(app), self.config(str(Path(tmp) / "ledger.jsonl"))))
            self.assertTrue(app.exists())
            self.assertIn("hook vetoed", notify.call_args.args[1])

    def test_permission_failure_uses_fallbacks_then_notifies_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Demo.app"
            app.mkdir()
            with patch.object(
                maintenance_core,
                "get_restore_source",
                return_value={"source": "homebrew", "restore_command": "brew install --cask demo"},
            ), patch.object(maintenance_core, "app_metadata", return_value={}), patch.object(
                maintenance_core, "run_delete_hooks", return_value=True
            ), patch.object(maintenance_core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(
                maintenance_core.time, "sleep", return_value=None
            ), patch.object(shutil, "move", side_effect=PermissionError("denied")), patch.object(
                maintenance_core, "trash_with_finder", return_value=False
            ) as finder, patch.object(
                maintenance_core, "trash_with_admin_mv", return_value=False
            ) as admin, patch.object(maintenance_core, "notify_user") as notify:
                self.assertFalse(maintenance_core.delete_app(str(app), self.config(str(Path(tmp) / "ledger.jsonl"))))
            finder.assert_called_once()
            admin.assert_called_once()
            self.assertIn("Could not move", notify.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
