"""Synthetic state only: no application launch, HID read, or private fixture."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge", ROOT / "bartender_context.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def state():
    return {"schema_version": 1, "last_return_flow_at": 1000,
            "health": {"last_sample_at": 1010, "return_routing_enabled": True},
            "incidents": [{"command": "private content must not be emitted"}]}


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.write(state())

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, obj):
        self.path.write_text(json.dumps(obj))


class SnapshotTests(StateFixture):
    def test_recent_return(self):
        value = b.snapshot(self.path, now=1020)
        self.assertTrue(value["idle"])
        self.assertEqual(value["remaining_seconds"], 100)

    def test_boundary_does_not_extend(self):
        self.write(dict(state(), health={"last_sample_at": 1110, "return_routing_enabled": True}))
        self.assertEqual(b.snapshot(self.path, now=1119)["remaining_seconds"], 1)
        self.assertFalse(b.snapshot(self.path, now=1120)["idle"])

    def test_stale_monitor(self):
        self.assertEqual(b.snapshot(self.path, now=1100)["reason"], "stale")

    def test_future_timestamp(self):
        self.assertFalse(b.snapshot(self.path, now=1009)["idle"])
        s = state(); s["last_return_flow_at"] = 1011; self.write(s)
        self.assertFalse(b.snapshot(self.path, now=1020)["idle"])

    def test_invalid_events(self):
        for stamp in (None, True, "1000", -1, float("nan"), 9999999):
            s = state(); s["last_return_flow_at"] = stamp; self.write(s)
            self.assertFalse(b.snapshot(self.path, now=1020)["idle"])

    def test_pending_is_not_a_return(self):
        s = state(); s["last_return_flow_at"] = 0; s["return_pending"] = True; self.write(s)
        self.assertFalse(b.snapshot(self.path, now=1020)["idle"])

    def test_disabled_unknown_and_bad_schema(self):
        for enabled in (False, None, "true", 1):
            s = state(); s["health"]["return_routing_enabled"] = enabled; self.write(s)
            self.assertFalse(b.snapshot(self.path, now=1020)["idle"])
        s = state(); s["schema_version"] = 2; self.write(s)
        self.assertEqual(b.snapshot(self.path, now=1020)["reason"], "schema")

    def test_failure_still_deserves_visibility(self):
        s = state(); s["return_health"] = {"last_error": "do not export", "last_error_at": 1000}; self.write(s)
        result = b.snapshot(self.path, now=1020)
        self.assertTrue(result["idle"])
        self.assertNotIn("export", json.dumps(result))

    def test_invalid_missing_oversize_and_symlink(self):
        self.path.write_text("{")
        self.assertFalse(b.snapshot(self.path, now=1020)["idle"])
        self.path.unlink(); self.assertFalse(b.snapshot(self.path, now=1020)["idle"])
        self.path.write_bytes(b"x" * (b.LIMIT + 1))
        self.assertFalse(b.snapshot(self.path, now=1020)["idle"])
        target = self.path.with_name("real.json"); target.write_text(json.dumps(state()))
        self.path.unlink(); self.path.symlink_to(target)
        self.assertFalse(b.snapshot(self.path, now=1020)["idle"])

    def test_no_sensitive_fields(self):
        result = b.snapshot(self.path, now=1020)
        self.assertEqual(set(result), {"schema", "monitor_fresh", "idle", "remaining_seconds", "reason", "pending", "sample_at"})
        self.assertNotIn("private", json.dumps(result))


class PublishTests(StateFixture):
    def setUp(self):
        super().setUp()
        self.controller = mock.Mock(SIGNALS=b.CHANNELS | {"idle"})
        self.request = {"schema": 1, "at": 1020, "meeting": True,
                        "events": [{"signal": "ai_cursor", "source": "job1", "expires_at": 1030}]}

    def test_bounded_publish(self):
        b.publish(self.request, self.controller, "root", self.path, now=1020)
        calls = self.controller.emit.call_args_list
        self.assertEqual(calls[0].args, ("root", "meeting", "hammerspoon.zoom", True, 30))
        self.assertEqual(calls[1].args, ("root", "idle", "idle-maintenance.return", True, 30))
        self.assertEqual(calls[2].args, ("root", "ai_cursor", "hammerspoon.event.job1", True, 10))

    def test_expired_job_is_cleared(self):
        self.request["events"][0]["expires_at"] = 1019
        b.publish(self.request, self.controller, "root", self.path, now=1020)
        self.assertFalse(self.controller.emit.call_args_list[-1].args[3])

    def test_validate_entire_batch_before_writing(self):
        bads = [dict(self.request, at=1000), dict(self.request, at=1030), dict(self.request, meeting="true"),
                dict(self.request, extra="not allowed"), dict(self.request, events=[{"signal": "bad"}])]
        for field, value in (("source", "../../escape"), ("signal", []), ("expires_at", float("inf")),
                             ("expires_at", 1321), ("expires_at", True)):
            request = copy.deepcopy(self.request); request["events"][0][field] = value; bads.append(request)
        request = copy.deepcopy(self.request); request["events"] *= 2; bads.append(request)
        for request in bads:
            with self.assertRaises(ValueError):
                b.publish(request, self.controller, "root", self.path, now=1020)
        self.controller.emit.assert_not_called()

    def test_empty_lua_table(self):
        request = dict(self.request, events={})
        self.assertEqual(b.validate_request(request, 1020)["events"], [])

    def test_old_controller_rejected(self):
        self.controller.SIGNALS = b.CHANNELS
        with self.assertRaises(ValueError):
            b.publish(self.request, self.controller, "root", self.path, now=1020)
        self.controller.emit.assert_not_called()

    def test_public_output_does_not_echo_bad_request(self):
        data = io.BytesIO(b'{"private":"sensitive content"}')
        stdin = mock.Mock(buffer=data)
        with mock.patch.object(b.sys, "stdin", stdin), mock.patch.object(b.sys, "stdout", new_callable=io.StringIO) as out:
            self.assertEqual(b.main(["publish"]), 1)
            self.assertNotIn("sensitive", out.getvalue())


class PendingEdgeTests(unittest.TestCase):
    def view(self, pending=False, sample=1000, healthy=True):
        return {"schema": 1, "monitor_fresh": healthy, "idle": False,
                "remaining_seconds": 0.0, "reason": "no-recent-return",
                "pending": pending, "sample_at": sample}

    def test_no_startup_synthetic_return(self):
        view, previous = b.pending_transition({}, self.view(True), 1000)
        self.assertFalse(view["idle"])
        view, _ = b.pending_transition(previous, self.view(True, 1010), 1010)
        self.assertFalse(view["idle"])

    def test_actual_pending_edge_is_bounded(self):
        _, previous = b.pending_transition({}, self.view(), 1000)
        view, previous = b.pending_transition(previous, self.view(True, 1010), 1010)
        self.assertTrue(view["idle"])
        self.assertEqual(view["remaining_seconds"], 120)
        for now in range(1020, 1140, 10):
            view, previous = b.pending_transition(previous, self.view(True, now), now)
            self.assertEqual(view["idle"], now < 1130)
        self.assertFalse(view["idle"])

    def test_stop_sleep_unknown_or_clock_reset_does_not_make_edge(self):
        for now, sample, healthy in ((1060,1060,True),(990,990,True),(1010,1010,False)):
            _, previous = b.pending_transition({}, self.view(), 1000)
            view, _ = b.pending_transition(previous, self.view(True, sample, healthy), now)
            self.assertFalse(view["idle"])

    def test_persisted_edge_survives_short_reload_without_resetting_window(self):
        _, previous = b.pending_transition({}, self.view(), 1000)
        _, previous = b.pending_transition(previous, self.view(True,1010), 1010)
        view, _ = b.pending_transition(json.loads(json.dumps(previous)), self.view(True,1020), 1020)
        self.assertTrue(view["idle"])
        self.assertEqual(view["remaining_seconds"],110)


if __name__ == "__main__":
    unittest.main()
