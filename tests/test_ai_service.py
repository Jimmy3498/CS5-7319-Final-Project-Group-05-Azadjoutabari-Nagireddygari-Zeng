import json
import unittest
from dataclasses import asdict
from pathlib import Path

from shared.codec import decode, load_json
from shared.domain.ai import AIService
from shared.domain.knowledge import KnowledgePolicy
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.provider import ScriptedTestProvider
from shared.models.ai import ProviderCapture, ProviderProfile, ProviderResponse
from shared.models.common import SecretInput
from shared.models.gameplay import SessionSnapshot, TurnProposal
from shared.models.world import CampaignDraft


class RecordingProvider(ScriptedTestProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def complete(self, messages, capture):
        self.requests.append(messages)
        return super().complete(messages, capture)


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        draft = decode(load_json(path.read_text(encoding="utf-8"))["draft"], CampaignDraft)
        self.state = SessionSnapshot(
            id="session",
            owner_id="owner",
            campaign_id="campaign",
            base_version_id="version",
            revision=0,
            player_character_id="rowan",
            world=draft.content.world,
            disclosed_ids=(),
            flag_values=tuple((f.id, f.initial) for f in draft.content.world.flags),
            setup=(draft.content.background, draft.content.opening),
        )
        policy = KnowledgePolicy()
        self.context = policy.director_context(self.state, "Look around")
        self.view = policy.player_view(self.state, (), "Look around")
        self.capture = ProviderCapture(
            ProviderProfile(
                "profile",
                "owner",
                "Test",
                "http://localhost",
                "test-model",
                "chat-completions-compatible",
                1,
                True,
            ),
            SecretInput("TEST-KEY-NOT-FOR-PROMPTS"),
        )
        self.plan = {
            "expected_revision": 0,
            "effects": [],
            "public_fact_ids": ["opening"],
        }

    def test_planning_and_narration_use_separate_contexts(self):
        provider = RecordingProvider(
            (
                ProviderResponse(json.dumps(self.plan), finish_reason="stop"),
                ProviderResponse("  You look around the hall.  ", finish_reason="stop"),
            )
        )
        service = AIService(provider)
        self.assertEqual(
            service.plan_turn(self.context, self.capture),
            TurnProposal(0, (), ("opening",)),
        )
        self.assertEqual(
            service.narrate(self.view, self.capture),
            "You look around the hall.",
        )
        self.assertEqual(provider.calls, 2)

        planner, narrator = provider.requests
        self.assertIn("SPOILER-CANARY-CEDRIC", planner[1].content)
        self.assertNotIn(
            "SPOILER-CANARY-CEDRIC",
            "".join(message.content for message in narrator),
        )
        self.assertEqual(
            json.loads(narrator[1].content),
            json.loads(json.dumps(asdict(self.view))),
        )
        for request in provider.requests:
            self.assertEqual([m.role for m in request], ["system", "user"])
            self.assertNotIn(
                self.capture.credential.value,
                "".join(m.content for m in request),
            )

    def test_invalid_plans_are_rejected_without_retry(self):
        invalid = [
            "not JSON",
            "{}",
            json.dumps(self.plan | {"extra": "unexpected"}),
            json.dumps(self.plan | {"expected_revision": True}),
            json.dumps(self.plan | {"expected_revision": 1}),
            json.dumps(
                self.plan
                | {
                    "effects": [
                        {
                            "kind": "move_character",
                            "actor_id": "rowan",
                            "destination_id": "antechamber",
                        }
                    ]
                    * 33
                }
            ),
            json.dumps(self.plan | {"public_fact_ids": ["opening"] * 65}),
        ]
        for content in invalid:
            with self.subTest(content=content[:80]):
                provider = RecordingProvider((ProviderResponse(content),))
                with self.assertRaises(ApplicationError) as caught:
                    AIService(provider).plan_turn(self.context, self.capture)
                self.assertEqual(caught.exception.code, ErrorCode.PROVIDER)
                self.assertEqual(provider.calls, 1)

    def test_narrator_rejects_private_context_before_calling_provider(self):
        provider = RecordingProvider(())
        with self.assertRaises(ApplicationError) as caught:
            AIService(provider).narrate(self.context, self.capture)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID)
        self.assertEqual(provider.requests, [])

    def test_empty_oversized_and_truncated_narration_is_rejected(self):
        responses = (
            ProviderResponse("  "),
            ProviderResponse("x" * (1024 * 1024 + 1)),
            ProviderResponse("Unfinished sentence", finish_reason="length"),
        )
        for response in responses:
            with self.subTest(reason=response.finish_reason):
                provider = RecordingProvider((response,))
                with self.assertRaises(ApplicationError) as caught:
                    AIService(provider).narrate(self.view, self.capture)
                self.assertEqual(caught.exception.code, ErrorCode.PROVIDER)
                self.assertEqual(provider.calls, 1)

    def test_provider_failure_is_not_retried(self):
        provider = RecordingProvider(())
        with self.assertRaises(ApplicationError) as caught:
            AIService(provider).narrate(self.view, self.capture)
        self.assertEqual(caught.exception.code, ErrorCode.PROVIDER)
        self.assertEqual(len(provider.requests), 1)
