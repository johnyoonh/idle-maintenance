from __future__ import annotations

import math
import unittest

from process_triage import triage_process


def guidance(**overrides):
    value = {
        "role": "Synthetic routine process",
        "recurrence_group": "synthetic-routine",
        "cpu_review_multiplier": 0,
        "io_review_multiplier": 0,
    }
    value.update(overrides)
    return value


class ProcessTriageHardeningTests(unittest.TestCase):
    def test_malformed_numeric_overrides_fall_back_to_defaults(self):
        proc = {
            "cpu_samples": [75, "invalid", math.nan],
            "io_samples": [
                {"total_mib_s": 30, "write_mib_s": 5},
                {"total_mib_s": "invalid", "write_mib_s": math.inf},
            ],
        }
        config = {
            "process_routine_review_multiplier": "invalid",
            "process_high_cpu_threshold": None,
            "process_high_io_total_mib_per_second": "invalid",
            "process_high_io_write_mib_per_second": math.nan,
        }

        result = triage_process(
            proc,
            guidance(cpu_review_multiplier="invalid", io_review_multiplier=math.inf),
            config,
            peak_total_mib_s="invalid",
            peak_write_mib_s=math.nan,
        )

        self.assertEqual("suppress", result["decision"])

    def test_numeric_strings_still_override_thresholds(self):
        proc = {"cpu_samples": [60, 58, 55]}
        config = {
            "process_routine_review_multiplier": "2",
            "process_high_cpu_threshold": "25",
        }

        result = triage_process(proc, guidance(), config)

        self.assertEqual("review", result["decision"])
        self.assertIn("CPU 60.0% >= 50.0%", result["reason"])

    def test_zero_threshold_still_disables_that_dimension(self):
        proc = {
            "cpu_samples": [500],
            "io_samples": [{"total_mib_s": 5, "write_mib_s": 1}],
        }
        config = {
            "process_high_cpu_threshold": 0,
            "process_high_io_total_mib_per_second": 20,
            "process_high_io_write_mib_per_second": 10,
            "process_routine_review_multiplier": 4,
        }

        result = triage_process(proc, guidance(), config)

        self.assertEqual("suppress", result["decision"])


if __name__ == "__main__":
    unittest.main()
