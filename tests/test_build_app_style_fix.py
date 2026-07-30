from __future__ import annotations

import unittest

import build_app_style_fix as style


class BuildAppStyleFixTests(unittest.TestCase):
    def test_warning_state_precedes_running_state(self):
        source = f"prefix\n{style.OLD_STATUS_COLOR_ORDER}\nsuffix"
        result = style.apply_style_fix(source)
        warning = result.index('normalized.contains("stale")')
        healthy = result.index('normalized.contains("healthy")')
        self.assertLess(warning, healthy)

    def test_marker_changes_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "status color marker"):
            style.apply_style_fix("missing")

    def test_duplicate_marker_fails_closed(self):
        source = style.OLD_STATUS_COLOR_ORDER * 2
        with self.assertRaisesRegex(ValueError, "found 2"):
            style.apply_style_fix(source)


if __name__ == "__main__":
    unittest.main()
