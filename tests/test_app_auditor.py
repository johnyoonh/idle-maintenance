from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import app_auditor


class AppAuditorTests(unittest.TestCase):
    def test_discovery_is_limited_to_two_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            top = root / "Top.app"
            nested = root / "Vendor" / "Nested.app"
            too_deep = root / "Vendor" / "Group" / "TooDeep.app"
            top.mkdir()
            nested.mkdir(parents=True)
            too_deep.mkdir(parents=True)

            result = app_auditor.discover_apps([str(root)])

        self.assertEqual(set(result), {str(top), str(nested)})

    def test_spotlight_metadata_is_batched_into_one_bounded_command(self):
        apps = ["/Applications/One.app", "/Applications/Two.app"]
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="2025-01-01 00:00:00 +0000\0(null)",
            )
        )
        cache = {}

        result = app_auditor.get_last_used_many(
            apps,
            {},
            cache,
            timeout=1.25,
            command_runner=runner,
        )

        runner.assert_called_once()
        command = runner.call_args.args[0]
        self.assertEqual(command[-2:], apps)
        self.assertEqual(runner.call_args.kwargs["timeout"], 1.25)
        self.assertEqual(result[apps[0]][1], "2025-01-01")
        self.assertEqual(result[apps[1]], (None, "Unknown"))
        self.assertEqual(cache[app_auditor.normalize_app_path(apps[0])], "2025-01-01")

    def test_metadata_timeout_uses_cache_without_retry(self):
        app = "/Applications/One.app"
        normalized = os.path.realpath(app)
        runner = Mock(side_effect=subprocess.TimeoutExpired(["mdls"], 0.1))

        result = app_auditor.get_last_used_many(
            [app],
            {},
            {normalized: "2024-03-04"},
            timeout=0.1,
            command_runner=runner,
        )

        runner.assert_called_once()
        self.assertEqual(result[app][1], "2024-03-04")

    def test_more_recent_spotlight_date_takes_precedence_over_older_app_usage(self):
        app = "/Applications/One.app"
        normalized = app_auditor.normalize_app_path(app)
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="2026-10-04 00:00:00 +0000",
            )
        )
        # Usage timestamp from 2026-07-01 (1782864000) is older than Spotlight 2026-10-04
        app_usage = {normalized: 1782864000.0}

        result = app_auditor.get_last_used_many(
            [app],
            app_usage,
            {},
            timeout=1.0,
            command_runner=runner,
        )

        self.assertEqual(result[app][1], "2026-10-04")

    def test_more_recent_app_usage_takes_precedence_over_older_spotlight_date(self):
        app = "/Applications/One.app"
        normalized = app_auditor.normalize_app_path(app)
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="2026-05-01 00:00:00 +0000",
            )
        )
        from datetime import datetime
        dt = datetime(2026, 10, 4, 12, 0)
        app_usage = {normalized: dt.timestamp()}

        result = app_auditor.get_last_used_many(
            [app],
            app_usage,
            {},
            timeout=1.0,
            command_runner=runner,
        )

        self.assertIn("observed", result[app][1])
        self.assertTrue(result[app][1].startswith("2026-10-04"))

    def test_same_day_spotlight_time_takes_precedence_over_older_app_usage(self):
        from datetime import datetime

        app = "/Applications/One.app"
        normalized = app_auditor.normalize_app_path(app)
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="2026-10-04 18:00:00 +0000",
            )
        )
        app_usage = {normalized: datetime(2026, 10, 4, 12, 0).timestamp()}

        result = app_auditor.get_last_used_many(
            [app],
            app_usage,
            {},
            timeout=1.0,
            command_runner=runner,
        )

        self.assertEqual(result[app][1], "2026-10-04")


if __name__ == "__main__":
    unittest.main()
