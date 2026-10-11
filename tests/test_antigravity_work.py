import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch


BINARY = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
COMMAND = BINARY + " --standalone --https_server_port 12345 --csrf_token synthetic-secret"
NOW = 1800000000.0


class Response:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.payload[:limit]


class AntigravityWorkTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / "scripts/antigravity_work.py"
        self.assertTrue(path.exists(), "bounded Antigravity probe is missing")
        spec = importlib.util.spec_from_file_location("antigravity_work", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def summary(self, **changes):
        value = {"status": "CASCADE_RUN_STATUS_RUNNING", "lastModifiedTime": "2027-01-15T08:00:00Z"}
        value.update(changes)
        return value

    def run_probe(self, data, commands=None):
        with patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response(data)
            return self.mod.probe(commands=[COMMAND] if commands is None else commands, now=NOW)

    def test_fresh_running_is_active_and_counts_only_verified_work(self):
        self.assertEqual(self.run_probe({"trajectorySummaries": {"synthetic": self.summary()}}),
                         {"active": True, "state": "observed", "count": 1})

    def test_empty_response_is_observed_inactive(self):
        self.assertEqual(self.run_probe({}), {"active": False, "state": "observed", "count": 0})

    def test_idle_waiting_cancelled_cloud_and_stale_are_inactive(self):
        cases = [{"status": status} for status in [0, 1, 3, 4, True, "RUNNING", "FUTURE_STATUS"]]
        cases += [{"waitingSteps": [{}]}, {"killed": True}, {"interrupted": True}, {"cloud": True},
                  {"lastModifiedTime": "2027-01-15T07:44:59Z"},
                  {"lastModifiedTime": "2027-01-15T08:00:06Z"}]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(self.run_probe({"trajectorySummaries": {"x": self.summary(**change)}})["active"])

    def test_numeric_running_and_freshness_boundaries(self):
        for timestamp in ["2027-01-15T07:45:00Z", "2027-01-15T08:00:05Z"]:
            self.assertTrue(self.run_probe({"trajectorySummaries": {"x": self.summary(status=2, lastModifiedTime=timestamp)}})["active"])

    def test_malformed_data_never_grants_awake(self):
        cases = [[], None, {"error": "synthetic"}, {"trajectorySummaries": []}, {"trajectorySummaries": {"x": None}},
                 {"trajectorySummaries": {"x": self.summary(lastModifiedTime="bad")}},
                 {"trajectorySummaries": {"x": self.summary(lastModifiedTime=None)}},
                 {"trajectorySummaries": {"x": self.summary(waitingSteps="bad")}},
                 {"trajectorySummaries": {"x": self.summary(killed="false")}}, b"not-json"]
        for data in cases:
            with self.subTest(data=data):
                result = self.run_probe(data)
                self.assertEqual(result, {"active": False, "state": "unknown", "count": 0})

    def test_unknown_extra_fields_do_not_override_status(self):
        self.assertFalse(self.run_probe({"trajectorySummaries": {"x": self.summary(status=1, hasActivity=True, notFullyIdle=True)}})["active"])

    def test_exact_process_match_and_missing_credentials(self):
        for commands in [[], ["python -c " + COMMAND], [COMMAND.replace(BINARY, BINARY + "-other")]]:
            self.assertEqual(self.run_probe({}, commands), {"active": False, "state": "absent", "count": 0})
        self.assertEqual(self.run_probe({}, [BINARY + " --standalone"])["state"], "unknown")

    def test_timeout_is_sanitized_and_no_private_output(self):
        output = io.StringIO()
        with patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("synthetic-secret private-title")
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = self.mod.probe(commands=[COMMAND], now=NOW)
        self.assertEqual(result, {"active": False, "state": "unknown", "count": 0})
        self.assertEqual(output.getvalue(), "")

    def test_process_limit_is_unknown_without_unbounded_requests(self):
        with patch.object(self.mod.urllib.request, "build_opener") as build:
            result = self.mod.probe(commands=[COMMAND] * 5, now=NOW)
            self.assertEqual(result, {"active": False, "state": "unknown", "count": 0})
            build.return_value.open.assert_not_called()

    def test_response_size_limit(self):
        result = self.run_probe(b" " * 2000001)
        self.assertEqual(result, {"active": False, "state": "unknown", "count": 0})

    def test_multiple_servers_aggregate_and_partial_failure_remains_unknown(self):
        with patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = [Response({}), Response({"trajectorySummaries": {"x": self.summary()}})]
            self.assertEqual(self.mod.probe(commands=[COMMAND, COMMAND.replace("12345", "12346")], now=NOW),
                             {"active": True, "state": "observed", "count": 1})
            build.return_value.open.side_effect = [Response({}), TimeoutError("private")]
            self.assertEqual(self.mod.probe(commands=[COMMAND, COMMAND.replace("12345", "12346")], now=NOW),
                             {"active": False, "state": "unknown", "count": 0})

    def test_local_request_contract_and_bounded_timeout(self):
        def check(request, timeout):
            self.assertEqual(request.full_url, "https://127.0.0.1:12345/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories")
            self.assertEqual(request.data, b"{}")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("X-codeium-csrf-token"), "synthetic-secret")
            self.assertLessEqual(timeout, 0.75)
            return Response({})
        with patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = check
            self.assertEqual(self.mod.probe(commands=[COMMAND], now=NOW)["state"], "observed")

    def test_default_process_snapshot_is_bounded_and_failure_unknown(self):
        with patch.object(self.mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertEqual(self.mod.probe(now=NOW), {"active": False, "state": "unknown", "count": 0})

    def test_redirects_cannot_escape_authenticated_loopback_endpoint(self):
        handler = self.mod._NoRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "redirect", {}, "https://example.invalid/")

    def test_ephemeral_port_discovery_uses_owned_pid_and_loopback_only(self):
        command = COMMAND.replace("12345", "0")
        snapshot = SimpleNamespace(stdout="42 " + command + "\n")
        listeners = SimpleNamespace(stdout="TCP 127.0.0.1:12344 (LISTEN)\nTCP 127.0.0.1:12345 (LISTEN)\nTCP *:9000 (LISTEN)\n")
        with patch.object(self.mod.subprocess, "run", side_effect=[snapshot, listeners]), \
                patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = [urllib.error.URLError("not TLS"), Response({})]
            self.assertEqual(self.mod.probe(commands=[command], now=NOW),
                             {"active": False, "state": "observed", "count": 0})

    def test_ephemeral_discovery_rejects_ambiguous_identity_and_excess_ports(self):
        command = COMMAND.replace("12345", "0")
        for snapshot, listeners in [
                ("42 other-command\n", ""),
                ("42 " + command + "\n43 " + command + "\n", ""),
                ("42 " + command + "\n", "\n".join("TCP 127.0.0.1:%d (LISTEN)" % n for n in range(12000, 12005)))]:
            with patch.object(self.mod.subprocess, "run", side_effect=[SimpleNamespace(stdout=snapshot), SimpleNamespace(stdout=listeners)]), \
                    patch.object(self.mod.urllib.request, "build_opener") as build:
                self.assertEqual(self.mod.probe(commands=[command], now=NOW)["state"], "unknown")
                build.return_value.open.assert_not_called()

    def test_four_request_budget_is_shared_across_servers(self):
        command = COMMAND.replace("12345", "0")
        other = command + " --synthetic-owner second"
        snapshot = SimpleNamespace(stdout="42 " + command + "\n43 " + other + "\n")
        listeners = SimpleNamespace(stdout="\n".join("TCP 127.0.0.1:%d (LISTEN)" % n for n in range(12000, 12003)))
        attempted = []
        def reply(request, timeout):
            attempted.append(request.full_url)
            if len(attempted) == 3:
                return Response({"trajectorySummaries": {"x": self.summary()}})
            if len(attempted) > 4:
                self.fail("request budget escaped")
            raise urllib.error.URLError("synthetic wrong listener")
        with patch.object(self.mod.subprocess, "run", side_effect=[snapshot, listeners, listeners]), \
                patch.object(self.mod.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = reply
            self.assertEqual(self.mod.probe(commands=[command, other], now=NOW),
                             {"active": False, "state": "unknown", "count": 0})
        self.assertEqual(len(attempted), 4)


if __name__ == "__main__":
    unittest.main()
