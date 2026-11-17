import tempfile
import unittest
from pathlib import Path

from scripts.risk.private_isolation import ARCHITECTURES, RAW_FIELDS, run_trial, write_results


class PrivateIsolationRiskTests(unittest.TestCase):
    def test_both_gateways_keep_private_canaries_out_of_player_boundaries(self):
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture):
                result = run_trial(architecture, 1)
                self.assertTrue(result.planner_contains_private_canary)
                self.assertFalse(result.narrator_contains_private_canary)
                self.assertFalse(result.public_outputs_contain_private_canary)
                self.assertFalse(result.trace_contains_private_canary)
                self.assertFalse(result.provider_messages_contain_credential_canary)
                self.assertFalse(result.public_outputs_contain_credential_canary)
                self.assertTrue(result.isolation_passed)

    def test_output_uses_boolean_evidence_without_canary_values(self):
        result = run_trial("Layered", 1)
        metadata = {"git": {"commit_sha": "test", "working_tree_dirty": True}}
        with tempfile.TemporaryDirectory() as temporary:
            raw_path, summary_path, summary = write_results(
                [result], Path(temporary), metadata=metadata
            )
            lines = raw_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(tuple(lines[0].split(",")), RAW_FIELDS)
            self.assertEqual(len(lines), 2)
            self.assertNotIn("SPOILER-CANARY", raw_path.read_text(encoding="utf-8"))
            self.assertNotIn("RISK-KEY-CANARY", raw_path.read_text(encoding="utf-8"))
            self.assertTrue(summary_path.is_file())
            self.assertTrue(summary["all_trials_passed"])


if __name__ == "__main__":
    unittest.main()
