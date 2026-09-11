import json
import tempfile
import unittest
from pathlib import Path

from leave_history_reconcile import reconcile_process_leave_history


class LeaveHistoryReconcileTests(unittest.TestCase):
    def _write_history(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )

    def test_recovers_repeated_keep_counts_for_all_affected_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            whitelist = root / "process_whitelist.json"
            families = (
                ["fileproviderd"] * 5
                + ["aw-server-rust"] * 2
                + ["corespotlightd"] * 3
                + ["WallpaperAerialsExtension"] * 2
            )
            rows = [
                {
                    "event": "prompted",
                    "action": "KEEP",
                    "timestamp": 1_000 + index,
                    "incident_id": f"incident-{index}",
                    "process": family,
                }
                for index, family in enumerate(families, start=1)
            ]
            rows.extend(
                [
                    dict(rows[0]),
                    {
                        "event": "prompted",
                        "action": "SNOOZE",
                        "timestamp": 9_999,
                        "incident_id": "ignored",
                        "process": "fileproviderd",
                    },
                ]
            )
            self._write_history(history, rows)

            result = reconcile_process_leave_history(
                history_path=history,
                whitelist_path=whitelist,
            )
            state = json.loads(whitelist.read_text(encoding="utf-8"))

            self.assertTrue(result["changed"])
            self.assertEqual(result["history_keep_events"], 12)
            self.assertEqual(state["process-family:fileproviderd"]["keep_count"], 5)
            self.assertEqual(state["process-family:aw-server-rust"]["keep_count"], 2)
            self.assertEqual(state["process-family:corespotlightd"]["keep_count"], 3)
            self.assertEqual(
                state["process-family:WallpaperAerialsExtension"]["keep_count"],
                2,
            )

    def test_reconciliation_is_idempotent_and_does_not_reduce_newer_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            whitelist = root / "process_whitelist.json"
            rows = [
                {
                    "event": "prompted",
                    "action": "KEEP",
                    "timestamp": 2_000 + index,
                    "incident_id": f"cursor-{index}",
                    "process": "Cursor",
                }
                for index in range(2)
            ]
            self._write_history(history, rows)

            first = reconcile_process_leave_history(
                history_path=history,
                whitelist_path=whitelist,
            )
            second = reconcile_process_leave_history(
                history_path=history,
                whitelist_path=whitelist,
            )
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])

            state = json.loads(whitelist.read_text(encoding="utf-8"))
            state["process-family:Cursor"] = {"kept_at": 9_000, "keep_count": 7}
            whitelist.write_text(json.dumps(state), encoding="utf-8")

            third = reconcile_process_leave_history(
                history_path=history,
                whitelist_path=whitelist,
            )
            preserved = json.loads(whitelist.read_text(encoding="utf-8"))
            self.assertFalse(third["changed"])
            self.assertEqual(
                preserved["process-family:Cursor"],
                {"kept_at": 9_000, "keep_count": 7},
            )

    def test_invalid_existing_whitelist_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.jsonl"
            whitelist = root / "process_whitelist.json"
            self._write_history(
                history,
                [
                    {
                        "event": "prompted",
                        "action": "KEEP",
                        "timestamp": 3_000,
                        "incident_id": "one",
                        "process": "fileproviderd",
                    }
                ],
            )
            whitelist.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(ValueError):
                reconcile_process_leave_history(
                    history_path=history,
                    whitelist_path=whitelist,
                )
            self.assertEqual(whitelist.read_text(encoding="utf-8"), "{not-json")


if __name__ == "__main__":
    unittest.main()
