import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location("ai_sleep", Path(__file__).parents[1] / "scripts/ai_sleep.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class PolicyTests(unittest.TestCase):
    def test_requires_work_and_internet_and_releases_after_grace(self):
        policy = mod.Policy()
        self.assertFalse(policy.decide(False, True, now=0))
        self.assertFalse(policy.decide(True, False, now=1))
        self.assertTrue(policy.decide(True, True, now=2))
        self.assertTrue(policy.decide(False, True, now=61))
        self.assertFalse(policy.decide(False, True, now=62))

    def test_offline_does_not_renew_and_reconnect_requires_active_work(self):
        p = mod.Policy()
        self.assertTrue(p.decide(True, True, now=100))
        self.assertTrue(p.decide(True, False, now=130))
        self.assertFalse(p.decide(True, False, now=161))
        self.assertFalse(p.decide(False, True, now=162))
        self.assertTrue(p.decide(True, True, now=163))

    def test_watchdog_rejects_expired_future_wrong_boot_dead_and_malformed_lease(self):
        good = {"boot": "boot", "expires": 180, "pid": 42, "disabled": True}
        with patch.object(mod.os, "kill", return_value=None):
            self.assertTrue(mod.valid_lease(good, "boot", now=100))
            for bad in [None, {}, dict(good, expires=99), dict(good, expires=1000), dict(good, boot="old"), dict(good, pid=-1), dict(good, disabled="true")]:
                self.assertFalse(mod.valid_lease(bad, "boot", now=100))
        with patch.object(mod.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(mod.valid_lease(good, "boot", now=100))

    def test_lease_uses_boot_shared_clock_instead_of_process_relative_clock(self):
        good = {"boot": "boot", "expires": 180, "pid": 42, "disabled": True}
        with patch.object(mod.time, "monotonic", return_value=0.01), patch.object(mod.time, "clock_gettime", return_value=100), patch.object(mod.os, "kill", return_value=None):
            self.assertTrue(mod.valid_lease(good, "boot"))

    def test_power_idempotence_and_resulting_state(self):
        power = mod.Power()
        with patch.object(power, "read", side_effect=[False, True]) as read, patch.object(mod, "run") as run:
            power.apply(True)
            run.assert_called_once_with(["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1"], timeout=5)
            self.assertEqual(read.call_count, 2)
        with patch.object(power, "read", return_value=False), patch.object(mod, "run") as run:
            power.apply(False)
            run.assert_not_called()
        with patch.object(power, "read", return_value=False), patch.object(mod, "run"):
            with self.assertRaises(OSError):
                power.apply(True)

    def test_controller_online_work_then_completion_releases_and_cleans_lease(self):
        from unittest.mock import Mock
        import json
        clock = [0]

        class Stop:
            def is_set(self):
                return clock[0] >= 120

            def set(self):
                clock[0] = 120

            def wait(self, interval):
                clock[0] += 30

        sources, power = Mock(), Mock()
        sources.inspect.side_effect = [{"codex": {"active": active}} for active in (True, False, False, False)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(mod.threading, "Event", return_value=Stop()), patch.object(mod.signal, "signal"), patch.object(mod, "shared_clock", side_effect=lambda: clock[0]), patch.object(mod, "internet_available", return_value=True) as internet:
                mod.controller(root, "boot", sources, power)
            self.assertEqual([c.args[0] for c in power.apply.call_args_list], [True, True, False, False, False])
            self.assertIsNone(json.loads((root / "lease.json").read_text()))
            internet.assert_called_once()

    def test_watchdog_and_duplicate_controller_ownership(self):
        from unittest.mock import Mock
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            root, power = Path(directory), Mock()
            mod.watchdog(root, "boot", power)
            power.apply.assert_called_once_with(False)
            with (root / "controller.lock").open("a") as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(SystemExit):
                    mod.controller(root, "boot", Mock(), power)

    def test_watchdog_preserves_healthy_active_lease(self):
        from unittest.mock import Mock
        import json
        with tempfile.TemporaryDirectory() as directory:
            root, power = Path(directory), Mock()
            (root / "lease.json").write_text(json.dumps({"boot": "boot", "pid": 42, "disabled": True, "expires": 180}))
            with patch.object(mod, "shared_clock", return_value=100), patch.object(mod.os, "kill", return_value=None):
                mod.watchdog(root, "boot", power)
            power.apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
