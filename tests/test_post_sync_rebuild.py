from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/post_sync_rebuild.sh"


class PostSyncRebuildTests(unittest.TestCase):
    def test_dry_run_resolves_identity_and_restart_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            security = bin_dir / "security"
            security.write_text(
                '#!/bin/bash\nprintf \'  1) ABCDEF "Apple Development: Example"\n\'\n',
                encoding="utf-8",
            )
            security.chmod(0o755)
            pgrep = bin_dir / "pgrep"
            pgrep.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            pgrep.chmod(0o755)
            result = subprocess.run(
                [str(SCRIPT), "--dry-run"],
                env=dict(
                    os.environ,
                    IDLE_MAINTENANCE_TEST_MODE="1",
                    IDLE_MAINTENANCE_SECURITY_BIN=str(security),
                    IDLE_MAINTENANCE_PGREP_BIN=str(pgrep),
                    IDLE_MAINTENANCE_LOG_DIR=str(root / "logs"),
                    IDLE_MAINTENANCE_POST_SYNC_LOCK=str(root / "lock"),
                    IDLE_MAINTENANCE_APP_DIR=str(root / "Applications"),
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CODESIGN_IDENTITY=ABCDEF", result.stdout)
            self.assertIn("restart=1", result.stdout)

    def test_loaded_resource_monitor_is_kickstarted_after_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            applications = root / "Applications"
            applications.mkdir()
            kickstart_marker = root / "kickstart-called"

            security = bin_dir / "security"
            security.write_text(
                '#!/bin/bash\nprintf \'  1) ABCDEF "Apple Development: Example"\n\'\n',
                encoding="utf-8",
            )
            security.chmod(0o755)
            pgrep = bin_dir / "pgrep"
            pgrep.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            launchctl = bin_dir / "launchctl"
            launchctl.write_text(
                "#!/bin/bash\n"
                "if [[ \"$1\" == \"print\" ]]; then exit 0; fi\n"
                f"printf '%s\\n' \"$*\" > {kickstart_marker!s}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            build = bin_dir / "build"
            build.write_text(
                '#!/bin/bash\nmkdir -p "$1/IdleMaintenance.app"\n',
                encoding="utf-8",
            )
            build.chmod(0o755)
            reconcile = root / "reconcile.py"
            reconcile.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(SCRIPT)],
                env=dict(
                    os.environ,
                    HOME=str(root),
                    IDLE_MAINTENANCE_TEST_MODE="1",
                    IDLE_MAINTENANCE_SECURITY_BIN=str(security),
                    IDLE_MAINTENANCE_PGREP_BIN=str(pgrep),
                    IDLE_MAINTENANCE_LAUNCHCTL_BIN=str(launchctl),
                    IDLE_MAINTENANCE_BUILD_SCRIPT=str(build),
                    IDLE_MAINTENANCE_RECONCILE_SCRIPT=str(reconcile),
                    IDLE_MAINTENANCE_UID="501",
                    IDLE_MAINTENANCE_LOG_DIR=str(root / "logs"),
                    IDLE_MAINTENANCE_POST_SYNC_LOCK=str(root / "lock"),
                    IDLE_MAINTENANCE_APP_DIR=str(applications),
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(kickstart_marker.exists())
            self.assertEqual(
                kickstart_marker.read_text(encoding="utf-8").strip(),
                "kickstart -k gui/501/com.john.idle-maintenance-monitor",
            )
            self.assertIn("restarting the loaded resource monitor", result.stdout)

    def test_missing_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                [str(SCRIPT), "--dry-run"],
                env=dict(
                    os.environ,
                    PATH="/usr/bin:/bin",
                    IDLE_MAINTENANCE_TEST_MODE="1",
                    IDLE_MAINTENANCE_SECURITY_BIN=str(root / "missing-security"),
                    IDLE_MAINTENANCE_LOG_DIR=str(root / "logs"),
                    IDLE_MAINTENANCE_POST_SYNC_LOCK=str(root / "lock"),
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("no Apple Development signing identity", result.stdout)

    def test_slow_shutdown_fails_without_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            applications = root / "Applications"
            applications.mkdir()
            marker = root / "open-called"

            security = bin_dir / "security"
            security.write_text(
                '#!/bin/bash\nprintf \'  1) ABCDEF "Apple Development: Example"\n\'\n',
                encoding="utf-8",
            )
            security.chmod(0o755)
            pgrep = bin_dir / "pgrep"
            pgrep.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            pgrep.chmod(0o755)
            pkill = bin_dir / "pkill"
            pkill.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            pkill.chmod(0o755)
            sleep = bin_dir / "sleep"
            sleep.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            sleep.chmod(0o755)
            open_bin = bin_dir / "open"
            open_bin.write_text(f"#!/bin/bash\ntouch {marker!s}\n", encoding="utf-8")
            open_bin.chmod(0o755)
            build = bin_dir / "build"
            build.write_text(
                '#!/bin/bash\nmkdir -p "$1/IdleMaintenance.app"\n',
                encoding="utf-8",
            )
            build.chmod(0o755)
            reconcile = root / "reconcile.py"
            reconcile.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(SCRIPT)],
                env=dict(
                    os.environ,
                    HOME=str(root),
                    IDLE_MAINTENANCE_TEST_MODE="1",
                    IDLE_MAINTENANCE_SECURITY_BIN=str(security),
                    IDLE_MAINTENANCE_PGREP_BIN=str(pgrep),
                    IDLE_MAINTENANCE_PKILL_BIN=str(pkill),
                    IDLE_MAINTENANCE_SLEEP_BIN=str(sleep),
                    IDLE_MAINTENANCE_OPEN_BIN=str(open_bin),
                    IDLE_MAINTENANCE_BUILD_SCRIPT=str(build),
                    IDLE_MAINTENANCE_RECONCILE_SCRIPT=str(reconcile),
                    IDLE_MAINTENANCE_RESTART_ATTEMPTS="2",
                    IDLE_MAINTENANCE_RESTART_SLEEP_SECONDS="0",
                    IDLE_MAINTENANCE_LOG_DIR=str(root / "logs"),
                    IDLE_MAINTENANCE_POST_SYNC_LOCK=str(root / "lock"),
                    IDLE_MAINTENANCE_APP_DIR=str(applications),
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("leaving the still-running process untouched", result.stdout)


if __name__ == "__main__":
    unittest.main()
