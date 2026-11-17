import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from event_based.bootstrap import build as build_events
from layered.bootstrap import build as build_layered
from shared.codec import decode, load_json
from shared.commands import RetryTurn
from shared.domain.gameplay import GameplayService
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.world import CampaignDraft


class InterruptedTurnTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = Mock()
        self.vault = Mock()
        for target, replacement in (
            ("shared.wiring.CompatibleHttpProvider", self.provider),
            ("shared.wiring.LocalCredentialStore", self.vault),
        ):
            patcher = patch(target, return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, root):
        store = SQLiteStore(root / "application.sqlite3")
        store.initialize()
        owner = store.create_account("Alice", "test-hash").id
        fixture = Path(__file__).resolve().parents[1] / "fixtures/thornmere.json"
        draft = decode(load_json(fixture.read_text())["draft"], CampaignDraft)
        created = store.create_campaign(owner, "Story", "Summary")
        saved = store.save_draft(owner, created.id, 0, draft.content)
        publication = store.publish(owner, saved.id, saved.revision)
        state = store.start_session(owner, publication, "rowan")
        return store, owner, state

    def rows(self, store):
        with closing(store._connect()) as db:
            return {
                row["operation_id"]: tuple(row)
                for row in db.execute(
                    """SELECT r.operation_id, r.status, r.public_error_code,
                              t.state, t.provider_outcome_uncertain, r.result_json
                       FROM operation_receipts r JOIN turn_attempts t
                       ON r.operation_id = t.operation_id ORDER BY r.operation_id"""
                )
            }

    def test_restart_fails_only_interrupted_turn_and_allows_explicit_retry(self):
        for build in (build_layered, build_events):
            with self.subTest(architecture=build.__module__):
                root = self.root / build.__module__
                store, owner, state = self.seed(root)
                app = build(root)
                store.admit_turn(owner, "completed", "request-0", state.id, 0, "Look")
                committed = store.commit_turn(owner, "completed", 0, state, "Look", "You look.")
                state = store.load_session(owner, state.id)
                store.admit_turn(owner, "rejected", "request-1", state.id, 1, "Invalid")
                store.record_attempt_failure(owner, "rejected", state.id, "invalid", False)
                store.admit_turn(owner, "interrupted", "request-2", state.id, 1, "Wait")
                before = self.rows(store)
                app.close()

                # A new host sees durable admission without a completion or failure.
                app = build(root)
                app.close()
                after = self.rows(store)
                self.assertEqual(after["completed"], before["completed"])
                self.assertEqual(after["rejected"], before["rejected"])
                self.assertEqual(
                    after["interrupted"][1:5],
                    ("failed", ErrorCode.PROVIDER_UNCERTAIN.value, "failed", 1),
                )
                self.assertEqual(store.load_session(owner, state.id), state)
                self.assertEqual(store.accepted_history(owner, state.id), (committed.turn,))
                self.provider.complete.assert_not_called()
                self.vault.read.assert_not_called()

                app = build(root)
                app.close()
                self.assertEqual(self.rows(store), after)  # Repeated startup is harmless.
                with self.assertRaises(ApplicationError) as late:
                    store.commit_turn(owner, "interrupted", 1, state, "Wait", "Late result")
                self.assertEqual(late.exception.code, ErrorCode.CONFLICT)

                # Explicit user retry creates the existing linked attempt; startup did not.
                retry = GameplayService(store, store).retry(
                    owner, RetryTurn("interrupted", state.id, 1), "retry", "request-3"
                )
                self.assertEqual(retry.player_input, "Wait")
                with closing(store._connect()) as db:
                    prior_id = db.execute(
                        "SELECT id FROM turn_attempts WHERE operation_id = 'interrupted'"
                    ).fetchone()[0]
                    self.assertEqual(
                        db.execute(
                            "SELECT prior_attempt_id FROM turn_attempts WHERE operation_id = 'retry'"
                        ).fetchone()[0],
                        prior_id,
                    )
                result = store.commit_turn(owner, "retry", 1, state, "Wait", "You wait.")
                self.assertEqual(result.turn.revision, 2)

    def test_restart_also_allows_a_new_action_in_the_same_session(self):
        for build in (build_layered, build_events):
            with self.subTest(architecture=build.__module__):
                root = self.root / build.__module__
                store, owner, state = self.seed(root)
                store.admit_turn(owner, "abandoned", "old", state.id, 0, "Old action")
                app = build(root)
                try:
                    admitted = store.admit_turn(owner, "new", "fresh", state.id, 0, "New action")
                    self.assertEqual(admitted.baseline, state)
                    self.assertEqual(store.accepted_history(owner, state.id), ())
                    self.provider.complete.assert_not_called()
                finally:
                    app.close()

    def test_recovery_updates_receipt_and_attempt_atomically(self):
        store, owner, state = self.seed(self.root)
        store.admit_turn(owner, "abandoned", "old", state.id, 0, "Wait")
        before = self.rows(store)
        with closing(store._connect()) as db:
            db.execute("""CREATE TRIGGER fail_recovery BEFORE UPDATE ON operation_receipts
                          BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            store.recover_interrupted_turns()
        self.assertEqual(self.rows(store), before)
        self.assertEqual(store.load_session(owner, state.id), state)
