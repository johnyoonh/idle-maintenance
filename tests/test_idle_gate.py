import unittest

from idle_watcher import review_gate_transition


class IdleGateTests(unittest.TestCase):
    def test_short_pause_waits_for_five_minutes_without_input(self):
        was_idle, pending, _should_review = review_gate_transition(
            601,
            was_idle=False,
            review_pending=False,
            away_seconds=600,
            active_cutoff_seconds=300,
            review_idle_seconds=300,
            review_idle_max_seconds=0,
        )
        was_idle, pending, _should_review = review_gate_transition(
            0,
            was_idle=was_idle,
            review_pending=pending,
            away_seconds=600,
            active_cutoff_seconds=300,
            review_idle_seconds=300,
            review_idle_max_seconds=0,
        )

        _was_idle, pending, should_review = review_gate_transition(
            299,
            was_idle=was_idle,
            review_pending=pending,
            away_seconds=600,
            active_cutoff_seconds=300,
            review_idle_seconds=300,
            review_idle_max_seconds=0,
        )
        self.assertFalse(should_review)
        self.assertTrue(pending)

        _was_idle, pending, should_review = review_gate_transition(
            300,
            was_idle=False,
            review_pending=pending,
            away_seconds=600,
            active_cutoff_seconds=300,
            review_idle_seconds=300,
            review_idle_max_seconds=0,
        )
        self.assertTrue(should_review)
        self.assertFalse(pending)

    def test_extended_idle_remains_eligible_without_an_upper_bound(self):
        _was_idle, pending, should_review = review_gate_transition(
            24 * 60 * 60,
            was_idle=True,
            review_pending=True,
            away_seconds=600,
            active_cutoff_seconds=300,
            review_idle_seconds=300,
            review_idle_max_seconds=0,
        )

        self.assertTrue(should_review)
        self.assertFalse(pending)

    def test_unknown_idle_sample_does_not_fake_a_return(self):
        try:
            result = review_gate_transition(
                None,
                was_idle=True,
                review_pending=True,
                away_seconds=600,
                active_cutoff_seconds=300,
                review_idle_seconds=300,
                review_idle_max_seconds=0,
            )
        except (TypeError, ValueError):
            result = None

        self.assertEqual(result, (True, True, False))


if __name__ == "__main__":
    unittest.main()
