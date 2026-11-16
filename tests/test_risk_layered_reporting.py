import tempfile
import unittest
from pathlib import Path

from scripts.risk.layered_reporting import (
    RAW_FIELDS,
    ScenarioConfig,
    run_benchmark,
    run_trial,
    write_results,
)
from shared.gateway import OperationStatus


class LayeredReportingRiskTests(unittest.TestCase):
    def test_successful_reporting_returns_completed_without_pending_flag(self):
        result = run_trial(ScenarioConfig("test_success", 0.0, False), 1)

        self.assertEqual(result.status, OperationStatus.COMPLETED.value)
        self.assertTrue(result.result_is_turn_view)
        self.assertIs(result.reporting_pending, False)
        self.assertEqual(result.commit_count, 1)
        self.assertEqual(result.committed_state_record_count, 1)
        self.assertEqual(result.reporting_success_count, 1)
        self.assertEqual(result.reporting_failure_count, 0)
        self.assertTrue(result.commit_before_reporting)
        self.assertTrue(result.commit_exactly_once)
        self.assertTrue(result.committed_state_preserved)
        self.assertTrue(result.sequence_valid)
        self.assertLess(
            result.call_sequence.index("commit"),
            result.call_sequence.index("report_start"),
        )

    def test_reporting_failure_preserves_commit_and_completed_gameplay_result(self):
        result = run_trial(ScenarioConfig("test_failure", 0.0, True), 1)

        self.assertEqual(result.status, OperationStatus.COMPLETED.value)
        self.assertTrue(result.result_is_turn_view)
        self.assertIs(result.reporting_pending, True)
        self.assertEqual(result.commit_count, 1)
        self.assertEqual(result.committed_state_record_count, 1)
        self.assertEqual(result.reporting_success_count, 0)
        self.assertEqual(result.reporting_failure_count, 1)
        self.assertTrue(result.commit_before_reporting)
        self.assertTrue(result.commit_exactly_once)
        self.assertTrue(result.committed_state_preserved)
        self.assertTrue(result.sequence_valid)
        self.assertIn("report_failure", result.call_sequence)

    def test_tiny_reporting_delay_is_measured_without_slowing_unit_tests(self):
        result = run_trial(ScenarioConfig("test_delay", 0.001, False), 1)
        row = result.raw_row()

        self.assertEqual(result.status, OperationStatus.COMPLETED.value)
        self.assertIs(result.reporting_pending, False)
        self.assertGreater(row["reporting_duration_ms"], 0)
        self.assertGreaterEqual(row["total_submit_response_ms"], row["reporting_duration_ms"])
        self.assertTrue(result.commit_exactly_once)

    def test_output_accounts_for_success_and_failure_scenarios(self):
        scenarios = (
            ScenarioConfig("test_success", 0.0, False),
            ScenarioConfig("test_failure", 0.0, True),
        )
        results = run_benchmark(scenarios, 1)
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
            self.assertIn("reporting simulation", summary["label"])
            self.assertIn("is implemented", summary["configuration"]["catch_up_status"])
            self.assertIn("outside this experiment", summary["configuration"]["catch_up_status"])


if __name__ == "__main__":
    unittest.main()
