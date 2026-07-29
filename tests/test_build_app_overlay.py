import unittest

import build_app_overlay as overlay


class BuildAppOverlayTests(unittest.TestCase):
    def fixture(self):
        return "\n".join([
            "prefix",
            overlay.OLD_MENU,
            overlay.OLD_SHORTCUT_ACTION,
            overlay.OLD_STATUS_SCRIPT,
            "suffix",
        ])

    def test_overlay_adds_io_sections_and_canonical_shortcut_command(self):
        result = overlay.apply_overlay(self.fixture())
        self.assertIn("Resource Activity", result)
        self.assertIn("Review Recent I/O Incidents…", result)
        self.assertIn("Sample CPU + Disk I/O (1 min)", result)
        self.assertIn("Refresh & Review Shortcuts", result)
        self.assertIn("Start / Restart Away-Return Review", result)
        self.assertIn('maintenanceDir.appendingPathComponent("maint.py").path', result)
        self.assertIn('"shortcuts"', result)
        self.assertIn("maintenance_status_extended.py", result)
        self.assertNotIn("Review Sustained High CPU", result)

    def test_overlay_fails_closed_when_upstream_marker_changes(self):
        with self.assertRaisesRegex(ValueError, "menu marker"):
            overlay.apply_overlay(self.fixture().replace(overlay.OLD_MENU, "changed"))

    def test_overlay_rejects_duplicate_markers(self):
        with self.assertRaisesRegex(ValueError, "menu marker"):
            overlay.apply_overlay(self.fixture() + "\n" + overlay.OLD_MENU)


if __name__ == "__main__":
    unittest.main()
