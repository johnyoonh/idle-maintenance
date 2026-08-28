from __future__ import annotations

import math
import unittest

from process_review import process_headline


class ProcessHeadlineTests(unittest.TestCase):
    def test_io_headline_reports_peak_cpu_not_last_sample(self):
        process = {
            "cpu_samples": [100, 20, 10],
            "io_samples": [
                {"total_mib_s": 30},
                {"total_mib_s": 80},
            ],
        }

        self.assertEqual(
            "I/O peak 80.0 MiB/s • CPU peak 100.0%",
            process_headline(process),
        )

    def test_headline_ignores_nonfinite_metrics(self):
        process = {
            "cpu_samples": [math.nan, 75, math.inf],
            "io_samples": [
                {"total_mib_s": math.inf},
                {"total_mib_s": 25},
            ],
        }

        self.assertEqual(
            "I/O peak 25.0 MiB/s • CPU peak 75.0%",
            process_headline(process),
        )


if __name__ == "__main__":
    unittest.main()
