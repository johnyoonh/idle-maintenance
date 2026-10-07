from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.watch_sysmond import sample_directory


class SampleDirectoryTests(unittest.TestCase):
    def test_rejects_existing_non_private_directory_without_changing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared"
            path.mkdir(mode=0o755)
            os.chmod(path, 0o755)

            with (
                patch("scripts.watch_sysmond.os.chown") as chown,
                patch("scripts.watch_sysmond.os.chmod") as chmod,
            ):
                with self.assertRaises(RuntimeError):
                    sample_directory(str(path), os.getuid(), os.getgid())

            chown.assert_not_called()
            chmod.assert_not_called()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
