from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AtomicBuildTests(unittest.TestCase):
    def fixture(self, directory: Path) -> tuple[Path, Path, Path]:
        source = directory / "source"
        destination = directory / "Applications"
        fake_bin = directory / "bin"
        source.mkdir()
        destination.mkdir()
        fake_bin.mkdir()
        shutil.copy2(ROOT / "build_app.sh", source / "build_app.sh")
        (source / "build_app_overlay.py").write_text(
            "import shutil, sys\nshutil.copy2(sys.argv[1], sys.argv[2])\n",
            encoding="utf-8",
        )
        (source / "build_app_core.sh").write_text(
            """#!/bin/bash
set -euo pipefail
[[ ${FAIL_CORE:-0} != 1 ]] || exit 41
root=$1
app=$root/IdleMaintenance.app
mkdir -p "$app/Contents/Resources/maintenance"
printf new > "$app/new-marker"
""",
            encoding="utf-8",
        )
        (source / "build_app_core.sh").chmod(0o755)
        for name in (
            "maintenance_core.py", "process_identity.py", "process_sampling.py",
            "process_review.py", "storage_cleanup_core.py", "disk_activity.py",
            "maint.py", "shortcut_review.py", "maintenance_status_extended.py",
        ):
            (source / name).write_text("# fixture\n", encoding="utf-8")
        (fake_bin / "codesign").write_text(
            "#!/bin/bash\n[[ ${FAIL_CODESIGN:-0} != 1 ]] || exit 42\nexit 0\n",
            encoding="utf-8",
        )
        (fake_bin / "codesign").chmod(0o755)
        (source / "build_app.sh").chmod(0o755)
        return source, destination, fake_bin

    def run_build(self, source: Path, destination: Path, fake_bin: Path, **extra):
        env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", **extra)
        return subprocess.run(
            [str(source / "build_app.sh"), str(destination)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def install_old(self, destination: Path) -> Path:
        app = destination / "IdleMaintenance.app"
        app.mkdir()
        (app / "old-marker").write_text("old", encoding="utf-8")
        return app

    def test_success_swaps_after_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination, fake_bin = self.fixture(Path(tmp))
            app = self.install_old(destination)
            result = self.run_build(source, destination, fake_bin)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((app / "new-marker").exists())
            self.assertFalse((app / "old-marker").exists())

    def test_compile_failure_preserves_installed_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination, fake_bin = self.fixture(Path(tmp))
            app = self.install_old(destination)
            result = self.run_build(source, destination, fake_bin, FAIL_CORE="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((app / "old-marker").exists())

    def test_signing_failure_preserves_installed_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination, fake_bin = self.fixture(Path(tmp))
            app = self.install_old(destination)
            result = self.run_build(source, destination, fake_bin, FAIL_CODESIGN="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((app / "old-marker").exists())

    def test_script_never_deletes_installed_path_directly(self):
        text = (ROOT / "build_app.sh").read_text(encoding="utf-8")
        self.assertNotIn('rm -rf "$APP_PATH"', text)
        self.assertIn('"$TMP_CORE" "$STAGE_ROOT"', text)
        self.assertLess(text.index('codesign --verify --deep --strict "$STAGED_APP"'), text.index('mv -- "$APP_PATH" "$BACKUP_PATH"'))


if __name__ == "__main__":
    unittest.main()
