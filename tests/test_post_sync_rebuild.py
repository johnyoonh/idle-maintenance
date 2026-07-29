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


if __name__ == "__main__":
    unittest.main()
