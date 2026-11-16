import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_profile_service import FakeVault

from layered.bootstrap import build
from shared import commands as c
from shared.codec import decode, load_json
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.provider import ScriptedTestProvider
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.ai import ProviderResponse
from shared.models.common import RequestContext, SecretInput
from shared.models.world import CampaignDraft
from shared.operation_status import OperationStatus


class LayeredTurnTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = ScriptedTestProvider(self.responses())

        for target, value in (
            ("shared.wiring.CompatibleHttpProvider", self.provider),
            ("shared.wiring.LocalCredentialStore", FakeVault()),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.app = build(self.root)
        self.addCleanup(self.app.close)
        self.context = RequestContext("browser", None, "turn-test")
        password = SecretInput("demo password")
        self.account = self.submit(c.RegisterAccount("Alice", password), "register")
        self.submit(c.Login("Alice", password), "login")

        card = self.submit(
            c.SaveProviderCard(
                None,
                None,
                "Test",
                "https://example.test/chat",
                "model",
                "chat-completions-compatible",
                SecretInput("FAKE-KEY"),
            ),
            "card",
        )
        self.submit(c.ActivateProviderCard(card.id, 1), "activate")

        path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        fixture = decode(load_json(path.read_text())["draft"], CampaignDraft)
        draft = self.submit(c.CreateCampaign("Story", "Summary"), "create")
        draft = self.submit(c.SaveDraft(draft.id, 0, fixture.content), "save")
        published = self.submit(c.PublishCampaign(draft.id, draft.revision), "publish")
        view = self.submit(c.StartSession(published.published_version_id, "rowan"), "start")

        self.store = SQLiteStore(self.root / "application.sqlite3")
        self.before = self.store.load_session(self.account.id, view.session_id)
        self.command = c.SubmitTurn(view.session_id, 0, "Go to the antechamber")

    def responses(self):
        return (
            ProviderResponse(
                json.dumps(
                    {
                        "expected_revision": 0,
                        "effects": [
                            {
                                "kind": "move_character",
                                "actor_id": "rowan",
                                "destination_id": "antechamber",
                            }
                        ],
                        "public_fact_ids": [],
                    }
                )
            ),
            ProviderResponse("You enter the antechamber."),
        )

    def submit(self, command, request_id):
        receipt = self.app.submit(self.context, command, request_id)
        self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
        return receipt.result

    def attempt(self, operation_id):
        with closing(self.store._connect()) as db:
            return db.execute(
                """
                SELECT state, provider_outcome_uncertain
                FROM turn_attempts WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()

    def test_turn_commits_and_replays_after_reopening(self):
        result = self.submit(self.command, "turn")
        self.assertFalse(result.reporting_pending)

        saved = self.store.load_session(self.account.id, self.before.id)
        self.assertEqual(saved.revision, 1)
        pc = next(c for c in saved.world.characters if c.id == "rowan")
        self.assertEqual(pc.location_id, "antechamber")
        self.assertEqual(
            self.store.accepted_history(self.account.id, saved.id),
            (result.turn,),
        )
        self.assertEqual(self.provider.calls, 2)

        self.assertEqual(self.submit(self.command, "turn").turn, result.turn)
        self.app.close()
        self.app = build(self.root)
        self.addCleanup(self.app.close)

        self.assertEqual(self.submit(self.command, "turn").turn, result.turn)
        self.assertEqual(self.provider.calls, 2)

        with closing(self.store._connect()) as db:
            count = db.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
            self.assertEqual(count, 1)
            db.execute("DELETE FROM consumer_receipts WHERE consumer_id = 'reporting'")
            db.commit()
        self.app.close()
        self.app = build(self.root)
        self.addCleanup(self.app.close)
        self.assertEqual(self.store.pending_notifications(), ())
        self.assertEqual(self.provider.calls, 2)

    def test_invalid_effect_releases_reservation(self):
        invalid = ProviderResponse(
            json.dumps(
                {
                    "expected_revision": 0,
                    "effects": [
                        {
                            "kind": "move_character",
                            "actor_id": "rowan",
                            "destination_id": "missing-location",
                        }
                    ],
                    "public_fact_ids": [],
                }
            )
        )
        transport = ScriptedTestProvider((invalid,))
        self.app.application.services.ai.transport = transport

        receipt = self.app.submit(self.context, self.command, "bad-turn")
        self.assertEqual(receipt.status, OperationStatus.REJECTED)
        self.assertEqual(self.attempt(receipt.operation_id)["state"], "rejected")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(
            self.store.load_session(self.account.id, self.before.id),
            self.before,
        )

        self.app.application.services.ai.transport = self.provider
        self.submit(self.command, "new-turn")
        self.assertEqual(self.provider.calls, 2)

    def test_narration_timeout_preserves_state_and_marks_uncertainty(self):
        responses = [
            self.responses()[0],
            ApplicationError(ErrorCode.PROVIDER_UNCERTAIN, "Timed out"),
        ]
        with patch.object(self.provider, "complete", side_effect=responses) as complete:
            receipt = self.app.submit(self.context, self.command, "timeout-turn")

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(receipt.status, OperationStatus.FAILED)
        attempt = self.attempt(receipt.operation_id)
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(attempt["provider_outcome_uncertain"], 1)
        self.assertEqual(
            self.store.load_session(self.account.id, self.before.id),
            self.before,
        )
        self.assertEqual(self.store.accepted_history(self.account.id, self.before.id), ())

    def test_explicit_retry_links_attempt_and_reuses_private_input(self):
        invalid = ProviderResponse(
            json.dumps(
                {
                    "expected_revision": 0,
                    "effects": [
                        {
                            "kind": "move_character",
                            "actor_id": "rowan",
                            "destination_id": "missing-location",
                        }
                    ],
                    "public_fact_ids": [],
                }
            )
        )
        self.app.application.services.ai.transport = ScriptedTestProvider((invalid,))
        failed = self.app.submit(self.context, self.command, "retry-source")
        self.assertEqual(failed.status, OperationStatus.REJECTED)

        self.app.application.services.ai.transport = self.provider
        retried = self.app.submit(
            self.context,
            c.RetryTurn(failed.operation_id, self.before.id, self.before.revision),
            "explicit-retry",
        )
        self.assertEqual(retried.status, OperationStatus.COMPLETED, retried.message)
        self.assertEqual(retried.result.turn.player_input, self.command.player_input)
        with closing(self.store._connect()) as db:
            link = db.execute(
                "SELECT prior_attempt_id FROM turn_attempts WHERE operation_id = ?",
                (retried.operation_id,),
            ).fetchone()[0]
        self.assertIsNotNone(link)
        report = self.store.campaign_report(
            self.account.id, self.before.campaign_id, self.before.base_version_id
        )
        self.assertEqual(report.accepted_turns, 1)
        self.assertEqual(report.rejected_turns, 1)
