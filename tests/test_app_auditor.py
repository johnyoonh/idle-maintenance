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
        from datetime import datetime, timezone

        app = "/Applications/One.app"
        normalized = app_auditor.normalize_app_path(app)
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="2026-10-04 18:00:00 +0000",
            )
        )
        app_usage = {normalized: datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc).timestamp()}

        result = app_auditor.get_last_used_many(
            [app],
            app_usage,
            {},
            timeout=1.0,
            command_runner=runner,
        )

        self.assertEqual(result[app][1], "2026-10-04")

    def test_excludes_local_automation_helpers_and_launchagent_apps(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_auto = root / "Applications" / "Local Automation"
            helper = local_auto / "Codex Automation Helper.app"
            helper.mkdir(parents=True)

            launch_agents = root / "LaunchAgents"
            launch_agents.mkdir()
            agent_app = root / "Applications" / "Git Sync.app"
            agent_app.mkdir(parents=True)
            plist_path = launch_agents / "com.example.gitsync.plist"
            with open(plist_path, "wb") as handle:
                plistlib.dump(
                    {"Label": "com.example.gitsync", "ProgramArguments": [str(agent_app / "Contents/MacOS/Runner")]},
                    handle,
                )

            regular_old = root / "Applications" / "OldApp.app"
            regular_old.mkdir(parents=True)
            old_ts = 1_700_000_000.0
            os.utime(regular_old, (old_ts, old_ts))

            launchagent_apps = app_auditor.launchagent_referenced_app_paths([str(launch_agents)])
            config = {"excluded_app_dirs": [str(local_auto)], "app_min_age_days": 14}

            self.assertTrue(app_auditor.is_excluded_app(str(helper), config, launchagent_apps))
            self.assertTrue(app_auditor.is_excluded_app(str(agent_app), config, launchagent_apps))
            # Freshly created directory has age ~0 (< 14 days), so also excluded when now_ts is near creation;
            # far in the future (> 14 days after creation), regular_old is NOT excluded.
            self.assertFalse(
                app_auditor.is_excluded_app(
                    str(regular_old),
                    config,
                    launchagent_apps,
                    now_ts=os.stat(regular_old).st_mtime + 30 * 86400,
                )
            )


if __name__ == "__main__":
    unittest.main()
