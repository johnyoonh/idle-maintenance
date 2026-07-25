import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from idle_config import atomic_write_json, disk_busy_status, parse_iostat_mib_per_second, read_json_file


class IdleConfigIOTests(unittest.TestCase):
    def test_parses_last_iostat_sample_across_disks(self):
        output = """
              disk0           disk2
        KB/t  tps  MB/s  KB/t  tps  MB/s
        8.00   10   0.1  8.00   10   0.2
        64.0  200  12.5  32.0  100   4.5
        """
        self.assertEqual(parse_iostat_mib_per_second(output), 17.0)

    def test_disk_busy_uses_configured_threshold(self):
        runner = lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="8 10 30\n",
            stderr="",
        )
        status = disk_busy_status(
            {"system_disk_busy_mib_per_second": 25, "system_disk_sample_seconds": 1},
            command_runner=runner,
            executable="/bin/true",
        )
        self.assertTrue(status["busy"])
        self.assertEqual(status["mib_per_second"], 30.0)

    def test_atomic_json_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertTrue(atomic_write_json(path, {"first": True}))
            self.assertTrue(atomic_write_json(path, {"second": True}))
            self.assertEqual(read_json_file(path), {"second": True})


if __name__ == "__main__":
    unittest.main()
