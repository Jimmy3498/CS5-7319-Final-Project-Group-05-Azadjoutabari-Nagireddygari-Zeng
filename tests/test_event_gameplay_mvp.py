import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_profile_service import FakeVault

from event_based.bootstrap import build
from event_based.messages import Topic
from shared import commands as c
from shared.codec import decode, load_json
from shared.gateway import OperationStatus
from shared.infrastructure.provider import ScriptedTestProvider
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.ai import ProviderResponse
from shared.models.common import RequestContext, SecretInput
from shared.models.reporting import CampaignReport
from shared.models.views import TurnResultView
from shared.models.world import CampaignDraft
from shared.queries import Query, QueryKind


class EventGameplayMvpTests(unittest.TestCase):
    """End-to-end Event-Based turn, durable notice, result, and report scenario."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = ScriptedTestProvider(
            (
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
        )
        for target, value in (
            ("shared.wiring.CompatibleHttpProvider", self.provider),
            ("shared.wiring.LocalCredentialStore", FakeVault()),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = build(self.root, start_worker=False)
        self.addCleanup(self.app.close)
        self.context = RequestContext("event-browser", None, "event-mvp")

    def submit(self, command, request_id):
        receipt = self.app.submit(self.context, command, request_id)
        while self.app.bus.pending:
            self.assertTrue(self.app.bus.dispatch_one())
        receipt = self.app.poll(self.context, receipt.operation_id)
        self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
        return receipt.result

    def test_turn_flows_through_stages_and_updates_milestone_report(self):
        password = SecretInput("demo password")
        account = self.submit(c.RegisterAccount("Alice", password), "register")
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
        fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        fixture = decode(load_json(fixture_path.read_text())["draft"], CampaignDraft)
        draft = self.submit(c.CreateCampaign("Story", "Summary"), "create")
        draft = self.submit(c.SaveDraft(draft.id, 0, fixture.content), "save")
        published = self.submit(c.PublishCampaign(draft.id, draft.revision), "publish")
        view = self.submit(c.StartSession(published.published_version_id, "rowan"), "start")

        result = self.submit(c.SubmitTurn(view.session_id, 0, "Go to the antechamber"), "turn")
        self.assertIsInstance(result, TurnResultView)
        self.assertEqual(result.turn.revision, 1)
        self.assertEqual(self.provider.calls, 2)
        replayed = self.submit(
            c.RetryTurn(result.turn.operation_id, view.session_id, 1), "retry-completed"
        )
        self.assertEqual(replayed.turn, result.turn)
        self.assertEqual(self.provider.calls, 2)

        topics = [topic for topic, _, _ in self.app.bus.trace]
        for topic in (
            Topic.TURN_REQUEST.value,
            Topic.TURN_CONTEXT.value,
            Topic.TURN_PLAN.value,
            Topic.TURN_VALIDATED.value,
            Topic.NARRATION.value,
            Topic.TURN_COMMITTED.value,
        ):
            self.assertIn(topic, topics)

        store = SQLiteStore(self.root / "application.sqlite3")
        state = store.load_session(account.id, view.session_id)
        self.assertEqual(state.revision, 1)
        self.assertEqual(store.pending_notifications(), ())
        report = self.app.query(self.context, Query(QueryKind.CAMPAIGN_REPORT, draft.id))
        self.assertIsInstance(report, CampaignReport)
        self.assertEqual(report.accepted_turns, 1)
        reached = next(
            marker for marker in report.milestones if marker.marker_id == "entered-antechamber"
        )
        self.assertEqual(reached.playthrough_count, 1)
        self.assertEqual(reached.distinct_account_count, 1)

        # Simulate commit-before-report acknowledgment, then restart. Recovery republishes
        # identifiers only and must not repeat either paid provider request or local effects.
        with closing(store._connect()) as database:
            database.execute("DELETE FROM consumer_receipts WHERE consumer_id = 'reporting'")
            database.commit()
        self.assertEqual(len(store.pending_notifications()), 1)
        self.app.close()
        self.app = build(self.root, start_worker=False)
        self.addCleanup(self.app.close)
        while self.app.bus.pending:
            self.assertTrue(self.app.bus.dispatch_one())
        self.assertEqual(store.pending_notifications(), ())
        self.assertEqual(len(store.accepted_history(account.id, view.session_id)), 1)
        self.assertEqual(self.provider.calls, 2)


if __name__ == "__main__":
    unittest.main()
