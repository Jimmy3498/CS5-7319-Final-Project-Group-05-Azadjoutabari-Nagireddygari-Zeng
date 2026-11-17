import tempfile
import unittest
from pathlib import Path

from scripts.risk.interrupted_admission import ARCHITECTURES, RAW_FIELDS, run_trial, write_results


class InterruptedAdmissionRiskTests(unittest.TestCase):
    def test_both_gateways_classify_interruption_and_retry_explicitly(self):
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture):
                result = run_trial(architecture, 1)
                self.assertEqual(result.receipt_before_restart, "running")
                self.assertEqual(result.attempt_before_restart, "running")
                self.assertEqual(result.receipt_after_restart, "failed")
                self.assertEqual(result.attempt_after_restart, "failed")
                self.assertTrue(result.uncertain_after_restart)
                self.assertEqual(result.provider_calls_before_retry, 0)
                self.assertEqual(result.provider_calls_after_retry, 2)
                self.assertTrue(result.retry_linked_to_interrupted_attempt)
                self.assertTrue(result.interrupted_version_matches_session)
                self.assertTrue(result.retry_version_matches_session)
                self.assertEqual(result.version_report_accepted_turns, 1)
                self.assertTrue(result.second_restart_stable)
                self.assertTrue(result.recovery_passed)

    def test_output_accounts_for_each_trial(self):
        result = run_trial("Event-Based", 1)
        metadata = {"git": {"commit_sha": "test", "working_tree_dirty": True}}
        with tempfile.TemporaryDirectory() as temporary:
            raw_path, summary_path, summary = write_results(
                [result], Path(temporary), metadata=metadata
            )
            lines = raw_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(tuple(lines[0].split(",")), RAW_FIELDS)
            self.assertEqual(len(lines), 2)
            self.assertTrue(summary_path.is_file())
            self.assertTrue(summary["all_trials_passed"])


if __name__ == "__main__":
    unittest.main()
