import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ai_work", Path(__file__).parents[1] / "scripts/ai_work.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.rollout = self.sessions / "rollout-test.jsonl"
        self.rollout.write_text("")

    def append(self, event):
        with self.rollout.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": event}}) + "\n")

    def test_codex_active_requires_open_rollout_and_unfinished_turn(self):
        probe = mod.CodexProbe(self.sessions)
        self.append("task_started")
        self.assertEqual(probe.inspect([self.rollout], now=self.rollout.stat().st_mtime), 1)
        self.append("task_complete")
        self.assertEqual(probe.inspect([self.rollout], now=self.rollout.stat().st_mtime), 0)
        self.assertEqual(probe.inspect([]), 0)

    def test_codex_cancel_stale_unknown_and_external_file_are_inactive(self):
        probe = mod.CodexProbe(self.sessions)
        self.append("task_started")
        self.assertEqual(probe.inspect([self.rollout], now=self.rollout.stat().st_mtime + 901), 0)
        self.append("turn_aborted")
        self.assertEqual(probe.inspect([self.rollout]), 0)
        self.rollout.write_text('{"type":"response_item","payload":{"text":"task_started"}}\n')
        self.assertEqual(probe.inspect([self.rollout]), 0)
        outside = self.root / "rollout-outside.jsonl"
        outside.write_text('{"type":"event_msg","payload":{"type":"task_started"}}\n')
        self.assertEqual(probe.inspect([outside]), 0)

    def test_cache_tracks_new_turn_and_truncation(self):
        probe = mod.CodexProbe(self.sessions)
        self.append("task_started")
        self.assertEqual(probe.inspect([self.rollout]), 1)
        self.append("task_complete")
        self.append("task_started")
        self.assertEqual(probe.inspect([self.rollout]), 1)
        self.rollout.write_text("{}\n")
        self.assertEqual(probe.inspect([self.rollout]), 0)

    def test_lsof_requires_codex_process_and_rollout(self):
        raw = f'p42\nccodex\nn{self.rollout}\np43\nccodex-code-mode\nn{self.rollout}\n'
        self.assertEqual(mod.open_codex_files(raw), [self.rollout])

    def test_claude_prompt_stop_permission_and_resume(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "birth"}
        payload = {"session_id": "synthetic", "hook_event_name": "UserPromptSubmit", "tool_use_id": "tool-one"}
        store.record("claude", payload, owner, now=100)
        processes = {42: {"birth": "birth", "command": "claude"}}
        self.assertEqual(store.active(processes, now=101), {"claude": 1, "cursor": 0})
        payload["hook_event_name"] = "PermissionRequest"
        store.record("claude", payload, owner, now=102)
        self.assertEqual(store.active(processes, now=103)["claude"], 0)
        payload["hook_event_name"] = "PostToolUse"
        store.record("claude", payload, owner, now=104)
        self.assertEqual(store.active(processes, now=105)["claude"], 1)
        payload["hook_event_name"] = "Stop"
        store.record("claude", payload, owner, now=106)
        self.assertEqual(store.active(processes, now=107)["claude"], 0)

    def test_cursor_requires_progress_and_preserves_generation_identity(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "birth"}
        processes = {42: {"birth": "birth", "command": "/Applications/Cursor.app/Contents/MacOS/Cursor"}}
        p = {"conversation_id": "synthetic", "generation_id": "one", "hook_event_name": "beforeSubmitPrompt"}
        store.record("cursor", p, owner, now=100)
        self.assertEqual(store.active(processes, now=101)["cursor"], 0)
        p["hook_event_name"] = "afterAgentThought"
        store.record("cursor", p, owner, now=102)
        self.assertEqual(store.active(processes, now=103)["cursor"], 1)
        p["generation_id"] = "two"
        store.record("cursor", p, owner, now=104)
        p.update(generation_id="one", hook_event_name="stop")
        store.record("cursor", p, owner, now=105)
        self.assertEqual(store.active(processes, now=106)["cursor"], 1)
        p["hook_event_name"] = "postToolUse"
        store.record("cursor", p, owner, now=107)  # delayed old progress cannot resurrect
        self.assertEqual(store.active(processes, now=108)["cursor"], 1)
        p.update(generation_id="two", hook_event_name="preToolUse")
        store.record("cursor", p, owner, now=109)
        self.assertEqual(store.active(processes, now=110)["cursor"], 0)

    def test_hook_stale_dead_reused_pid_and_other_boot_fail_closed(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        store.record("claude", {"session_id": "s", "hook_event_name": "UserPromptSubmit"}, {"pid": 42, "birth": "a"}, now=100)
        self.assertEqual(store.active({}, now=101)["claude"], 0)
        self.assertEqual(store.active({42: {"birth": "b", "command": "claude"}}, now=101)["claude"], 0)
        self.assertEqual(store.active({42: {"birth": "a", "command": "claude"}}, now=1001)["claude"], 0)
        self.assertEqual(mod.HookStore(store.root, "other").active({42: {"birth": "a", "command": "claude"}}, now=101)["claude"], 0)

    def test_hooks_store_only_lifecycle_metadata(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        store.record("claude", {"session_id": "synthetic-private-id", "hook_event_name": "UserPromptSubmit", "prompt": "PRIVATE_TEXT", "cwd": "/private/work"}, {"pid": 42, "birth": "a"}, now=100)
        raw = next(store.root.glob("*.json")).read_text()
        for private in ("synthetic-private-id", "PRIVATE_TEXT", "/private/work"):
            self.assertNotIn(private, raw)

    def test_contended_stop_is_durable_and_reconciled(self):
        import fcntl
        store = mod.HookStore(self.root / "hooks", "boot")
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        store.record("claude", p, owner, now=100)
        with (store.root / "hooks.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p["hook_event_name"] = "Stop"
            store.record("claude", p, None, now=101)
        self.assertEqual(store.active(procs, now=102)["claude"], 0)

    def test_approval_can_suspend_even_when_owner_lookup_is_unavailable(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        store.record("claude", p, {"pid": 42, "birth": "a"}, now=100)
        p["hook_event_name"] = "PermissionRequest"
        store.record("claude", p, None, now=101)
        self.assertEqual(store.active({42: {"birth": "a", "command": "claude"}}, now=102)["claude"], 0)

    def test_delayed_progress_cannot_resurrect_stop_or_waiting(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        store.record("claude", p, owner, now=100)
        p["hook_event_name"] = "Stop"
        store.record("claude", p, owner, now=102)
        p["hook_event_name"] = "PostToolUse"
        store.record("claude", p, owner, now=103)
        self.assertEqual(store.active(procs, now=104)["claude"], 0)
        p["hook_event_name"] = "UserPromptSubmit"
        store.record("claude", p, owner, now=105)
        self.assertEqual(store.active(procs, now=106)["claude"], 1)
        p.update(conversation_id="s", generation_id="g", hook_event_name="preToolUse")
        store.record("cursor", p, owner, now=107)
        p["hook_event_name"] = "afterAgentThought"
        store.record("cursor", p, owner, now=108)
        cursor = {42: {"birth": "a", "command": "/Applications/Cursor.app/Contents/MacOS/Cursor"}}
        self.assertEqual(store.active(cursor, now=109)["cursor"], 0)

    def test_codex_approval_and_user_input_require_matching_resume(self):
        probe = mod.CodexProbe(self.sessions)
        self.append("task_started")
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "exec_approval_request", "call_id": "a"}}) + "\n")
        self.assertEqual(probe.inspect([self.rollout]), 0)
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "exec_command_begin", "call_id": "a"}}) + "\n")
        self.assertEqual(probe.inspect([self.rollout]), 1)

        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "functions.request_user_input", "arguments": "{}", "call_id": "b"}}) + "\n")
        self.assertEqual(probe.inspect([self.rollout]), 0)
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "unrelated"}}) + "\n")
        self.assertEqual(probe.inspect([self.rollout]), 0)
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "b"}}) + "\n")
        self.assertEqual(probe.inspect([self.rollout]), 1)
    def test_parallel_tool_returns_only_clear_matching_waits(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "/Applications/Cursor.app/Contents/MacOS/Cursor"}}
        p = {"conversation_id": "c", "generation_id": "g", "hook_event_name": "preToolUse", "tool_use_id": "one"}
        store.record("cursor", p, owner, now=100)
        p["tool_use_id"] = "two"
        store.record("cursor", p, owner, now=101)
        p.update(hook_event_name="postToolUse", tool_use_id="one")
        store.record("cursor", p, owner, now=102)
        self.assertEqual(store.active(procs, now=103)["cursor"], 0)
        p["tool_use_id"] = "unrelated"
        store.record("cursor", p, owner, now=104)
        self.assertEqual(store.active(procs, now=105)["cursor"], 0)
        p["tool_use_id"] = "two"
        store.record("cursor", p, owner, now=106)
        self.assertEqual(store.active(procs, now=107)["cursor"], 1)

    def test_session_end_cutoff_survives_late_ingestion_and_new_prompt_reopens(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        p = {"session_id": "s", "hook_event_name": "SessionEnd"}
        store.record("claude", p, None, now=102)
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        p["hook_event_name"] = "UserPromptSubmit"
        store.record("claude", p, owner, now=100)
        self.assertEqual(store.active(procs, now=103)["claude"], 0)
        store.record("claude", p, owner, now=104)
        self.assertEqual(store.active(procs, now=105)["claude"], 1)

    def test_claude_ask_user_question_waits_for_matching_answer(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        store.record("claude", p, owner, now=100)
        p.update(hook_event_name="PreToolUse", tool_name="AskUserQuestion", tool_use_id="question")
        store.record("claude", p, owner, now=101)
        self.assertEqual(store.active(procs, now=102)["claude"], 0)
        p["hook_event_name"] = "PostToolUse"
        store.record("claude", p, owner, now=103)
        self.assertEqual(store.active(procs, now=104)["claude"], 1)

    def test_claude_api_failure_ends_turn_and_elicitation_waits(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        store.record("claude", p, owner, now=100)
        p["hook_event_name"] = "Elicitation"
        store.record("claude", p, None, now=101)
        self.assertEqual(store.active(procs, now=102)["claude"], 0)
        p["hook_event_name"] = "PostToolBatch"
        store.record("claude", p, owner, now=103)
        self.assertEqual(store.active(procs, now=104)["claude"], 1)
        p["hook_event_name"] = "StopFailure"
        store.record("claude", p, None, now=105)
        p["hook_event_name"] = "PostToolBatch"
        store.record("claude", p, owner, now=106)
        self.assertEqual(store.active(procs, now=107)["claude"], 0)

    def test_unidentified_claude_wait_requires_complete_tool_batch(self):
        store = mod.HookStore(self.root / "hooks", "boot")
        owner = {"pid": 42, "birth": "a"}
        procs = {42: {"birth": "a", "command": "claude"}}
        p = {"session_id": "s", "hook_event_name": "UserPromptSubmit"}
        store.record("claude", p, owner, now=100)
        p["hook_event_name"] = "PermissionRequest"
        store.record("claude", p, None, now=101)
        p.update(hook_event_name="PostToolUse", tool_use_id="any-tool")
        store.record("claude", p, owner, now=102)
        self.assertEqual(store.active(procs, now=103)["claude"], 0)
        p["hook_event_name"] = "PostToolBatch"
        store.record("claude", p, owner, now=104)
        self.assertEqual(store.active(procs, now=105)["claude"], 1)



if __name__ == "__main__":
    unittest.main()
