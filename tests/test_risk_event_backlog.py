import tempfile
import unittest
from pathlib import Path

from scripts.risk.event_backlog import RAW_FIELDS, run_backlog_condition, write_results


class EventBacklogRiskTests(unittest.TestCase):
    def test_real_worker_dispatches_each_event_once_and_populates_measurements(self):
        result = run_backlog_condition(
            0.0,
            1,
            event_count=5,
            timeout_seconds=3.0,
            experiment_id="E1-test",
        )

        rows = result.raw_rows()
        self.assertFalse(result.timed_out)
        self.assertEqual(result.errors, ())
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({row["event_id"] for row in rows}), 5)
        self.assertEqual(len({row["operation_id"] for row in rows}), 5)
        self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4, 5])
        self.assertLessEqual(result.maximum_observed_pending_depth, 5)

        for row in rows:
            with self.subTest(event_id=row["event_id"]):
                self.assertTrue(row["completed"])
                self.assertEqual(row["handler_invocations"], 1)
                self.assertEqual(row["incomplete_reason"], "")
                self.assertIsNotNone(row["publication_completed_ms"])
                self.assertIsNotNone(row["handler_start_ms"])
                self.assertIsNotNone(row["handler_finish_ms"])
                self.assertIsNotNone(row["queue_wait_ms"])
                self.assertIsNotNone(row["processing_ms"])
                self.assertIsNotNone(row["end_to_end_ms"])
                self.assertGreaterEqual(row["queue_wait_ms"], 0)
                self.assertGreaterEqual(row["processing_ms"], 0)
                self.assertGreaterEqual(row["end_to_end_ms"], row["processing_ms"])

    def test_csv_and_json_outputs_have_the_declared_configuration_and_fields(self):
        result = run_backlog_condition(
            0.0,
            1,
            event_count=3,
            timeout_seconds=3.0,
            experiment_id="E1-test",
        )
        metadata = {
            "python_version": "test",
            "git": {"commit_sha": "test", "working_tree_dirty": True},
        }

        with tempfile.TemporaryDirectory() as temporary:
            raw_path, summary_path, summary = write_results(
                [result], Path(temporary), metadata=metadata
            )

            self.assertTrue(raw_path.is_file())
            self.assertTrue(summary_path.is_file())
            header = raw_path.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(tuple(header.split(",")), RAW_FIELDS)
            self.assertEqual(summary["experiment_id"], "E1")
            self.assertEqual(summary["configuration"]["worker_count"], 1)
            self.assertEqual(summary["conditions"][0]["processed_events"], 3)
            self.assertIn("connector-level", summary["scope"])


if __name__ == "__main__":
    unittest.main()
