import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("install_ai_sleep", Path(__file__).parents[1] / "scripts/install_ai_sleep.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class InstallTests(unittest.TestCase):
    def test_preserves_hook_configuration_and_is_idempotent(self):
        for kind in ("claude", "cursor"):
            data = {"custom": {"keep": True}, "hooks": {"stop": [{"command": "existing"}]}}
            mod.merge_hooks(data, kind, "observer")
            first = repr(data)
            mod.merge_hooks(data, kind, "observer")
            self.assertEqual(first, repr(data))
            self.assertEqual(data["custom"], {"keep": True})
            self.assertEqual(data["hooks"]["stop"][0], {"command": "existing"})

    def test_stop_waits_for_launchd_removal_before_rebootstrap(self):
        with patch.object(mod.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=0), Mock(returncode=0), Mock(returncode=1)]) as run, patch.object(mod.time, "sleep") as sleep:
            mod.stop_service("gui/501/example")
        self.assertEqual(run.call_args_list[1].args[0], ["/bin/launchctl", "bootout", "gui/501/example"])
        sleep.assert_called_once_with(0.2)

    def test_malformed_hook_config_is_rejected(self):
        for data in ({"hooks": []}, {"hooks": {"Stop": "wrong"}}):
            with self.assertRaises(ValueError):
                mod.merge_hooks(data, "claude", "observer")


if __name__ == "__main__":
    unittest.main()
