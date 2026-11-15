import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.codec import decode, load_json
from shared.commands import SubmitTurn
from shared.domain.gameplay import GameplayService
from shared.domain.knowledge import KnowledgePolicy
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.gameplay import Effect, EffectKind, TurnProposal
from shared.models.world import CampaignDraft


class TurnCommitTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "game.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.initialize()
        # These tests exercise persistence, not password authentication.
        self.owner = self.store.create_account("Alice", "test-hash").id

        path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        fixture = decode(load_json(path.read_text(encoding="utf-8"))["draft"], CampaignDraft)
        draft = self.store.create_campaign(self.owner, "Story", "Summary")
        draft = self.store.save_draft(self.owner, draft.id, 0, fixture.content)
        published = self.store.publish(self.owner, draft.id, draft.revision)
        self.state = self.store.start_session(self.owner, published, "rowan")

        self.service = GameplayService(self.store, self.store)
        self.command = SubmitTurn(self.state.id, 0, "Walk to the antechamber")
        admission = self.service.prepare(self.owner, self.command, "operation-1", "request-1")
        proposal = TurnProposal(
            0,
            (
                Effect(
                    EffectKind.MOVE_CHARACTER,
                    "rowan",
                    destination_id="antechamber",
                ),
            ),
            (),
        )
        candidate = self.service.validate_and_project(admission.baseline, proposal)
        self.candidate = KnowledgePolicy().authorize_disclosures(self.state, candidate, proposal)

    def commit(self):
        return self.service.commit(
            self.owner,
            "operation-1",
            self.command,
            self.candidate,
            "You enter the antechamber.",
        )

    def test_commit_survives_reopening_and_replays_once(self):
        result = self.commit()
        reopened = SQLiteStore(self.path)
        self.assertEqual(
            reopened.load_session(self.owner, self.state.id),
            replace(self.candidate, revision=1),
        )
        self.assertEqual(reopened.accepted_history(self.owner, self.state.id), (result.turn,))

        service = GameplayService(reopened, reopened)
        replay = service.prepare(self.owner, self.command, "another-operation", "request-1")
        self.assertIsNone(replay.baseline)
        self.assertEqual(replay.previous_commit, result)
        self.assertEqual(self.commit(), result)

        with closing(sqlite3.connect(self.path)) as db:
            for table in ("accepted_turns", "session_changes", "pending_notifications"):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                    1,
                )
            notice = load_json(
                db.execute("SELECT identifiers_json FROM pending_notifications").fetchone()[0]
            )
            self.assertEqual(
                set(notice),
                {
                    "event_id",
                    "operation_id",
                    "session_id",
                    "campaign_id",
                    "base_version_id",
                    "revision",
                },
            )
            self.assertEqual(notice["event_id"], result.notice.event_id)

    def test_failed_write_rolls_back_everything(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""
                CREATE TRIGGER fail_notice BEFORE INSERT ON pending_notifications
                BEGIN SELECT RAISE(ABORT, 'test failure'); END
            """)
            db.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.commit()

        self.assertEqual(self.store.load_session(self.owner, self.state.id), self.state)
        with closing(sqlite3.connect(self.path)) as db:
            for table in ("accepted_turns", "session_changes", "pending_notifications"):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                    0,
                )
            self.assertEqual(
                db.execute("SELECT count(*) FROM session_checkpoints").fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT status FROM operation_receipts").fetchone()[0],
                "running",
            )
            self.assertEqual(
                db.execute("SELECT state FROM turn_attempts").fetchone()[0],
                "running",
            )
            db.execute("DROP TRIGGER fail_notice")
            db.commit()

        self.assertEqual(self.commit().turn.revision, 1)

    def test_failed_attempt_rejects_late_completion(self):
        self.service.record_failure(
            self.owner,
            "operation-1",
            self.state.id,
            ErrorCode.PROVIDER_UNCERTAIN,
            True,
        )
        with self.assertRaises(ApplicationError) as caught:
            self.commit()
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)
        self.assertEqual(self.store.load_session(self.owner, self.state.id), self.state)
        self.assertEqual(self.service.history(self.owner, self.state.id), ())

        # A separate new request can proceed; this is not the retry workflow.
        self.service.prepare(self.owner, self.command, "operation-2", "request-2")
        result = self.service.commit(
            self.owner,
            "operation-2",
            self.command,
            self.candidate,
            "You enter the antechamber.",
        )
        self.assertEqual(result.turn.revision, 1)

    def test_mismatched_commit_is_rejected(self):
        cases = (
            (
                self.owner,
                replace(self.command, session_id="another-session"),
                self.candidate,
                ErrorCode.INVALID,
            ),
            (
                self.owner,
                replace(self.command, player_input="Different action"),
                self.candidate,
                ErrorCode.CONFLICT,
            ),
            ("outsider", self.command, self.candidate, ErrorCode.FORBIDDEN),
            (
                self.owner,
                self.command,
                replace(self.candidate, revision=1),
                ErrorCode.CONFLICT,
            ),
        )
        for owner, command, candidate, expected in cases:
            with self.subTest(expected=expected, owner=owner):
                with self.assertRaises(ApplicationError) as caught:
                    self.service.commit(owner, "operation-1", command, candidate, "Narration")
                self.assertEqual(caught.exception.code, expected)

        self.assertEqual(self.store.load_session(self.owner, self.state.id), self.state)
