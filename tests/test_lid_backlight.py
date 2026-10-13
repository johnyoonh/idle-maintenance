import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("lid_backlight", Path(__file__).parents[1] / "scripts/lid_backlight.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Display:
    def __init__(self):
        self.level = 0.7
        self.writes = []
        self.fail = False

    def get(self, display):
        return self.level

    def set(self, display, level):
        if self.fail:
            raise OSError("unavailable")
        self.writes.append((display, level))
        self.level = level


class LidBacklightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.display = Display()
        self.guard = mod.Guard(self.path, self.display)

    def test_close_enforces_zero_and_reopen_restores_once(self):
        self.guard.tick(True, 7)
        self.assertEqual(self.display.level, 0)
        self.display.level = 0.2  # ambient brightness changed while closed
        self.guard.tick(True, 7)
        self.guard.tick(False, 7)
        self.guard.tick(False, 7)
        self.assertEqual(self.display.writes, [(7, 0), (7, 0), (7, 0.7)])

    def test_restart_retains_restore_level(self):
        self.guard.tick(True, 7)
        restarted = mod.Guard(self.path, self.display)
        restarted.tick(True, 7)
        restarted.tick(False, 7)
        self.assertEqual(self.display.level, 0.7)

    def test_unknown_lid_or_missing_display_changes_nothing(self):
        self.guard.tick(None, 7)
        self.guard.tick(True, None)
        self.assertEqual(self.display.writes, [])

    def test_failed_zero_keeps_saved_level_for_retry(self):
        self.display.fail = True
        with self.assertRaises(OSError):
            self.guard.tick(True, 7)
        self.display.fail = False
        self.guard.tick(True, 7)
        self.guard.tick(False, 7)
        self.assertEqual(self.display.level, 0.7)

    def test_restore_does_not_override_manual_brightness_on_open(self):
        self.guard.tick(True, 7)
        self.display.level = 0.4
        self.guard.tick(False, 7)
        self.assertEqual(self.display.level, 0.4)

    def test_failed_restore_is_retained_for_restart_retry(self):
        self.guard.tick(True, 7)
        self.display.fail = True
        with self.assertRaises(OSError):
            self.guard.tick(False, 7)
        self.display.fail = False
        mod.Guard(self.path, self.display).tick(False, 7)
        self.assertEqual(self.display.level, 0.7)

    def test_failed_save_does_not_zero_display(self):
        self.guard.path = Path(self.temp.name) / "missing" / "state.json"
        with self.assertRaises(OSError):
            self.guard.tick(True, 7)
        self.assertEqual(self.display.writes, [])

    def test_never_apply_saved_brightness_to_different_display(self):
        self.guard.tick(True, 7)
        self.guard.tick(False, 8)
        self.assertEqual(self.display.writes, [(7, 0)])

    def test_corrupt_state_fails_without_overwriting(self):
        self.path.write_text('{"display":7,"brightness":2}')
        with self.assertRaises(ValueError):
            mod.Guard(self.path, self.display)
        self.assertIn('2', self.path.read_text())


if __name__ == "__main__":
    unittest.main()
