from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
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

    def test_lost_wakeup_race_closed_during_worker_exit_and_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            app_actions.enqueue_trash_action(
                "/Applications/First.app",
                state_path=state_path,
                lock_path=state_lock,
                now=1,
                job_id="job-1",
            )
            processed = []

            original_claim = app_actions._claim_next_job
            first_empty_observed = [False]

            def hook_claim(**kwargs):
                claimed = original_claim(**kwargs)
                if claimed is None and not first_empty_observed[0]:
                    first_empty_observed[0] = True
                    app_actions.enqueue_trash_action(
                        "/Applications/Second.app",
                        state_path=state_path,
                        lock_path=state_lock,
                        now=2,
                        job_id="job-2",
                    )
                return claimed

            with patch("app_actions._claim_next_job", side_effect=hook_claim):
                app_actions.run_worker(
                    state_path=state_path,
                    state_lock_path=state_lock,
                    worker_lock_path=worker_lock,
                    execute=lambda job: processed.append(job["id"]) or {"state": "completed", "result": {"outcome": "trashed"}},
                    now_fn=lambda: 100,
                )

            # Successor worker runs (as detached by launch_worker)
            app_actions.run_worker(
                state_path=state_path,
                state_lock_path=state_lock,
                worker_lock_path=worker_lock,
                execute=lambda job: processed.append(job["id"]) or {"state": "completed", "result": {"outcome": "trashed"}},
                now_fn=lambda: 101,
            )

            self.assertIn("job-1", processed)
            self.assertIn("job-2", processed)
            status = app_actions.app_action_status(state_path=state_path, lock_path=state_lock, now=102)
            self.assertEqual(status["queued"], 0, "No jobs should remain pending/lost")

    def test_successor_worker_retries_and_claims_after_lock_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, state_lock, worker_lock = self.paths(Path(tmp))
            app_actions.enqueue_trash_action(
                "/Applications/Pending.app",
                state_path=state_path,
                lock_path=state_lock,
                now=1,
                job_id="job-handoff",
            )
            processed = []

            # Hold worker lock externally to simulate Worker 1 finishing
            with open(worker_lock, "a+", encoding="utf-8") as held_lock:
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)

                sleep_count = [0]
                def fake_sleep(duration):
                    sleep_count[0] += 1
                    if sleep_count[0] >= 2:
                        fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

                app_actions.run_worker(
                    state_path=state_path,
                    state_lock_path=state_lock,
                    worker_lock_path=worker_lock,
                    execute=lambda job: processed.append(job["id"]) or {"state": "completed", "result": {"outcome": "trashed"}},
                    now_fn=lambda: 100,
                    retries=5,
                    retry_delay=0.01,
                    sleep_fn=fake_sleep,
                )

            self.assertEqual(processed, ["job-handoff"])
            status = app_actions.app_action_status(state_path=state_path, lock_path=state_lock, now=101)
            self.assertEqual(status["queued"], 0)

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

    def test_permission_failure_skips_finder_and_notifies_after_admin_failure(self):
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
                maintenance_core, "trash_with_admin_mv", return_value=False
            ) as admin, patch.object(maintenance_core, "notify_user") as notify:
                self.assertFalse(maintenance_core.delete_app(str(app), self.config(str(Path(tmp) / "ledger.jsonl"))))
            admin.assert_called_once()
            self.assertIn("Could not move", notify.call_args.args[1])

    def test_app_trash_never_automates_finder(self):
        source = Path(maintenance_core.__file__).read_text(encoding="utf-8")
        self.assertNotIn('tell application "Finder"', source)
        self.assertNotIn("trash_with_finder", source)

    def test_delete_app_does_not_use_broad_pkill(self):
        source = Path(maintenance_core.__file__).read_text(encoding="utf-8")
        # Ensure delete_app does not run broad pkill
        self.assertNotIn('subprocess.run(["pkill"', source)

    def test_terminate_app_processes_app_identity_scoped_and_fallback(self):
        app_path = "/Applications/Demo.app"
        fake_ps_output = (
            "101 /Applications/Demo.app/Contents/MacOS/Demo\n"
            "102 /Applications/Demo.app/Contents/Frameworks/Demo Helper.app/Contents/MacOS/Demo Helper\n"
            "201 /usr/local/bin/Demo\n"
            "202 /usr/bin/python3 /tmp/scripts/watch.py /Applications/Demo.app\n"
            "203 /usr/bin/vim /Applications/Demo.app/Contents/Info.plist\n"
        )
        signals_sent = []
        alive_pids = {101, 102, 201, 202, 203}

        def fake_signal(pid, sig):
            signals_sent.append((pid, sig))
            if sig == signal.SIGTERM:
                # 101 terminates on SIGTERM, 102 ignores SIGTERM
                if pid == 101:
                    alive_pids.discard(101)
            elif sig == signal.SIGKILL:
                alive_pids.discard(pid)
            elif sig == 0:
                if pid not in alive_pids:
                    raise ProcessLookupError()

        current_time = [0.0]

        def fake_monotonic():
            return current_time[0]

        def fake_sleep(duration):
            current_time[0] += duration

        maintenance_core.terminate_app_processes(
            app_path,
            cleanup_config={"terminate_grace_seconds": 1.0, "terminate_poll_seconds": 0.1},
            ps_runner=lambda: fake_ps_output,
            signal_fn=fake_signal,
            sleep_fn=fake_sleep,
            monotonic_fn=fake_monotonic,
        )

        # 101 should receive SIGTERM and not SIGKILL
        self.assertIn((101, signal.SIGTERM), signals_sent)
        self.assertNotIn((101, signal.SIGKILL), signals_sent)

        # 102 should receive SIGTERM and fallback to SIGKILL
        self.assertIn((102, signal.SIGTERM), signals_sent)
        self.assertIn((102, signal.SIGKILL), signals_sent)

        # Unrelated processes (201, 202, 203) must NEVER receive any signal
        unrelated_signals = [item for item in signals_sent if item[0] in {201, 202, 203}]
        self.assertEqual(unrelated_signals, [], "Unrelated processes must remain untouched")


if __name__ == "__main__":
    unittest.main()
