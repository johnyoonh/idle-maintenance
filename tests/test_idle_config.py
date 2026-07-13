import unittest
from datetime import datetime

from idle_config import is_terminal_suggestion_time, next_keep_delay_days


class IdleConfigTests(unittest.TestCase):
    def test_default_terminal_hours_use_exclusive_end(self):
        config = {"terminal_suggestion_start_hour": 9, "terminal_suggestion_end_hour": 21}

        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 9)))
        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 20)))
        self.assertFalse(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 21)))

    def test_overnight_terminal_hours(self):
        config = {"terminal_suggestion_start_hour": 22, "terminal_suggestion_end_hour": 6}

        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 23)))
        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 2, 5)))
        self.assertFalse(is_terminal_suggestion_time(config, datetime(2026, 1, 2, 12)))

    def test_equal_terminal_hours_mean_always(self):
        config = {"terminal_suggestion_start_hour": 7, "terminal_suggestion_end_hour": 7}

        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 3)))

    def test_invalid_hours_fall_back_to_defaults(self):
        config = {"terminal_suggestion_start_hour": 99, "terminal_suggestion_end_hour": "bad"}

        self.assertTrue(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 10)))
        self.assertFalse(is_terminal_suggestion_time(config, datetime(2026, 1, 1, 22)))

    def test_next_keep_delay_uses_existing_count(self):
        config = {
            "keep_days_limit": 30,
            "keep_backoff_multiplier": 2,
            "keep_backoff_max_days": 365,
        }

        self.assertEqual(next_keep_delay_days(config, None), 30)
        self.assertEqual(next_keep_delay_days(config, {"kept_at": 1, "keep_count": 2}), 120)


if __name__ == "__main__":
    unittest.main()
