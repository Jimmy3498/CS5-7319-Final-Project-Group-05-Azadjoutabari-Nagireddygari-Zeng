import tempfile
import unittest
from pathlib import Path

from scripts.risk.event_shutdown import (
    CONTROLLED_SHUTDOWN,
    NORMAL_COMPLETION,
    RAW_FIELDS,
    run_controlled_shutdown,
    run_normal_completion,
    write_results,
)


class EventShutdownRiskTests(unittest.TestCase):
    def test_normal_completion_dispatches_every_submitted_event_once(self):
        result = run_normal_completion()
        submitted = {message.id for message in result.submitted}

        self.assertEqual(result.scenario, NORMAL_COMPLETION)
        self.assertEqual(len(submitted), 10)
        self.assertEqual(len(result.processed_event_ids), 10)
        self.assertEqual(set(result.processed_event_ids), submitted)
        self.assertEqual(len(set(result.processed_event_ids)), 10)
        self.assertEqual(result.failures, ())
        self.assertEqual(result.discarded_event_ids, ())
        self.assertEqual(result.restored_event_ids, ())
        self.assertEqual(result.pending_before_action, 0)
        self.assertEqual(result.final_queue_depth, 0)
        self.assertGreaterEqual(result.close_time_ns, 0)

    def test_controlled_shutdown_discards_all_pending_events_without_restoration(self):
        result = run_controlled_shutdown()
        submitted = {message.id for message in result.submitted}
        discarded = set(result.discarded_event_ids)

        self.assertEqual(result.scenario, CONTROLLED_SHUTDOWN)
        self.assertEqual(len(submitted), 10)
        self.assertEqual(result.processed_event_ids, ())
        self.assertEqual(len(result.failures), 10)
        self.assertEqual(discarded, submitted)
        self.assertEqual(len(discarded), 10)
        self.assertEqual(result.pending_before_action, 10)
        self.assertEqual(result.final_queue_depth, 0)
        self.assertEqual(result.fresh_bus_queue_depth, 0)
        self.assertEqual(result.restored_event_ids, ())
        self.assertGreaterEqual(result.close_time_ns, 0)
        for failure in result.failures:
            self.assertEqual(failure.component, "shutdown")
            self.assertEqual(failure.category, "queue_closed_before_dispatch")
            self.assertFalse(hasattr(failure, "exception_text"))

    def test_outputs_account_for_every_event_without_sensitive_error_text(self):
        results = [run_normal_completion(3), run_controlled_shutdown(3)]
        metadata = {
            "python_version": "test",
            "git": {"commit_sha": "test", "working_tree_dirty": True},
        }
        with tempfile.TemporaryDirectory() as temporary:
            raw_path, summary_path, summary = write_results(
                results, Path(temporary), metadata=metadata
            )
            header = raw_path.read_text(encoding="utf-8").splitlines()[0]
            raw_text = raw_path.read_text(encoding="utf-8")

            self.assertTrue(summary_path.is_file())
            self.assertEqual(tuple(header.split(",")), RAW_FIELDS)
            self.assertEqual(len(raw_text.splitlines()), 7)
            self.assertNotIn("Queue closed before dispatch", raw_text)
            self.assertEqual(summary["scenarios"][0]["events_successfully_dispatched"], 3)
            self.assertEqual(summary["scenarios"][1]["events_discarded_during_shutdown"], 3)
            self.assertEqual(summary["scenarios"][1]["events_restored_in_fresh_bus"], 0)
            self.assertIn("not an unexpected crash", summary["scope"])


if __name__ == "__main__":
    unittest.main()
