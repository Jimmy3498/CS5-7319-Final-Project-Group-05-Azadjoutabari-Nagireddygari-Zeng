import tempfile
import unittest
from pathlib import Path

from scripts.risk.commit_interrupt import ARCHITECTURES, RAW_FIELDS, run_trial, write_results


class CommitInterruptRiskTests(unittest.TestCase):
    def test_both_gateways_recover_once_without_provider_replay(self):
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture):
                result = run_trial(architecture, 1)
                self.assertEqual(result.pending_notices_before_restart, 1)
                self.assertEqual(result.consumer_receipts_before_restart, 0)
                self.assertEqual(result.pending_notices_after_restart, 0)
                self.assertEqual(result.consumer_receipts_after_restart, 2)
                self.assertEqual(result.state_revision_after_restart, 1)
                self.assertEqual(result.accepted_turns_after_restart, 1)
                self.assertEqual(result.provider_calls_before_restart, 2)
                self.assertEqual(result.provider_calls_after_restart, 2)
                self.assertTrue(result.recovery_passed)

    def test_output_accounts_for_each_recovery_trial(self):
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
