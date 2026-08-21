import tempfile
import unittest
from pathlib import Path

import app_actions


class ActiveAppActionTests(unittest.TestCase):
    def test_duplicate_active_path_reuses_existing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = str(root / "actions.json")
            lock = str(root / "actions.lock")
            first = app_actions.enqueue_trash_action(
                "/Applications/Demo.app",
                state_path=state,
                lock_path=lock,
                now=1,
                job_id="first",
            )
            second = app_actions.enqueue_trash_action(
                "/Applications/Demo.app",
                state_path=state,
                lock_path=lock,
                now=2,
                job_id="second",
            )
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(app_actions.active_action_paths(state_path=state, lock_path=lock, now=2), {"/Applications/Demo.app"})
            self.assertEqual(len(app_actions._load_state_unlocked(state)["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
