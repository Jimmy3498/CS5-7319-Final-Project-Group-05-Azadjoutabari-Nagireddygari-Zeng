import tempfile
import unittest
from pathlib import Path

from scripts.risk.layered_latency import (
    RAW_FIELDS,
    run_benchmark,
    run_trial,
    write_results,
)
from shared.gateway import OperationStatus

EXPECTED_SEQUENCE = (
    "authorize",
    "prepare",
    "capture_profile",
    "history",
    "director_context",
    "plan_start",
    "plan_finish",
    "validate",
    "authorize_disclosures",
    "player_view",
    "narrate_start",
    "narrate_finish",
    "commit",
    "report",
)


class LayeredLatencyRiskTests(unittest.TestCase):
    def test_real_gateway_runs_sequential_ai_calls_then_commits_once(self):
        result = run_trial(0.0, 1)
        row = result.raw_row()

        self.assertEqual(result.status, OperationStatus.COMPLETED.value)
        self.assertTrue(result.result_is_turn_view)
        self.assertEqual(result.call_sequence, EXPECTED_SEQUENCE)
        self.assertTrue(result.sequence_valid)
        self.assertEqual(result.ai_call_count, 2)
        self.assertEqual(result.commit_count, 1)
        self.assertEqual(result.reporting_call_count, 1)
        self.assertGreaterEqual(row["planning_duration_ms"], 0)
        self.assertGreaterEqual(row["narration_duration_ms"], 0)
        self.assertGreaterEqual(row["submission_to_commit_ms"], 0)
        self.assertGreaterEqual(row["total_submit_response_ms"], row["submission_to_commit_ms"])
        self.assertGreaterEqual(row["return_after_commit_ms"], 0)

    def test_tiny_controlled_delays_are_recorded_without_slowing_unit_tests(self):
        result = run_trial(0.001, 1)
        row = result.raw_row()

        self.assertEqual(result.status, OperationStatus.COMPLETED.value)
        self.assertTrue(result.sequence_valid)
        self.assertGreater(row["planning_duration_ms"], 0)
        self.assertGreater(row["narration_duration_ms"], 0)
        self.assertEqual(result.ai_call_count, 2)
        self.assertEqual(result.commit_count, 1)

    def test_output_contains_all_trials_and_declared_fields(self):
        results = run_benchmark((0.0,), 2)
        metadata = {
            "python_version": "test",
            "git": {"commit_sha": "test", "working_tree_dirty": True},
        }
        with tempfile.TemporaryDirectory() as temporary:
            raw_path, summary_path, summary = write_results(
                results, Path(temporary), metadata=metadata
            )
            lines = raw_path.read_text(encoding="utf-8").splitlines()

            self.assertTrue(summary_path.is_file())
            self.assertEqual(tuple(lines[0].split(",")), RAW_FIELDS)
            self.assertEqual(len(lines), 3)
            self.assertEqual(
                summary["label"],
                "Layered coordinator simulation using controlled AI-service delays",
            )
            self.assertEqual(summary["conditions"][0]["completed_receipts"], 2)
            self.assertEqual(summary["conditions"][0]["total_ai_calls"], 4)
            self.assertEqual(summary["conditions"][0]["total_commits"], 2)


if __name__ == "__main__":
    unittest.main()
