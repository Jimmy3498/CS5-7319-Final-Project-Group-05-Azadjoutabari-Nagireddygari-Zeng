import unittest
from dataclasses import replace
from pathlib import Path

from event_based.gateway import EventGateway
from layered.application import LayeredApplication
from layered.gateway import LayeredGateway
from shared.codec import decode, dump_json, load_json
from shared.commands import RequestEditorProposal, ReviewEditorProposal, SaveDraft
from shared.domain.ai import AIService
from shared.domain.editor import EditorService
from shared.domain.services import Services
from shared.domain.world import WorldService
from shared.errors import ApplicationError, ErrorCode
from shared.gateway import OperationStatus
from shared.infrastructure.provider import ScriptedTestProvider
from shared.models.accounts import AccountView
from shared.models.ai import EditorCompletion, ProviderCapture, ProviderProfile, ProviderResponse
from shared.models.common import RequestContext, SecretInput
from shared.models.world import CampaignDraft, EditorBuffer, EditorProposal

ROOT = Path(__file__).resolve().parents[1]
AUTHOR_ID = "author-1"


def fixture() -> CampaignDraft:
    value = load_json((ROOT / "fixtures" / "thornmere.json").read_text())
    draft = decode(value["draft"], CampaignDraft)
    return replace(draft, owner_id=AUTHOR_ID)


def improved_content(draft: CampaignDraft):
    location = draft.content.world.locations[0]
    description = replace(
        location.description,
        text=(
            "Candlelight trembles across the storm-dark hall while wedding guests "
            "exchange guarded looks after the toast."
        ),
    )
    improved_location = replace(location, description=description)
    world = replace(
        draft.content.world,
        locations=(improved_location, *draft.content.world.locations[1:]),
    )
    return replace(draft.content, world=world)


class MemoryCreativeWritingRepository:
    def __init__(self, draft: CampaignDraft) -> None:
        self.draft = draft
        self.proposals: dict[str, tuple[str, EditorProposal]] = {}

    def load_draft(self, owner_id: str, campaign_id: str) -> CampaignDraft:
        if owner_id != self.draft.owner_id or campaign_id != self.draft.id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Draft is not owned by this account")
        return self.draft

    def save_draft(
        self,
        owner_id: str,
        campaign_id: str,
        expected_revision: int,
        content,
    ) -> CampaignDraft:
        current = self.load_draft(owner_id, campaign_id)
        if current.revision != expected_revision:
            raise ApplicationError(ErrorCode.CONFLICT, "Draft revision changed")
        self.draft = replace(current, revision=current.revision + 1, content=content)
        return self.draft

    def save_editor_proposal(self, owner_id: str, proposal: EditorProposal) -> None:
        self.load_draft(owner_id, proposal.campaign_id)
        self.proposals[proposal.id] = (owner_id, proposal)

    def load_editor_proposal(self, owner_id: str, proposal_id: str) -> EditorProposal:
        stored_owner, proposal = self.proposals[proposal_id]
        if stored_owner != owner_id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Proposal is not owned by this account")
        return proposal

    def resolve_editor_proposal(
        self, owner_id: str, proposal_id: str, status: str
    ) -> EditorProposal:
        proposal = self.load_editor_proposal(owner_id, proposal_id)
        resolved = replace(proposal, status=status)
        self.proposals[proposal_id] = (owner_id, resolved)
        return resolved


class StubAccounts:
    def require_user(self, context: RequestContext) -> AccountView:
        if context.user_id != AUTHOR_ID:
            raise ApplicationError(ErrorCode.UNAUTHENTICATED, "Sign in is required")
        return AccountView(AUTHOR_ID, "author")


class StubProfiles:
    def capture(self, owner_id: str) -> ProviderCapture:
        if owner_id != AUTHOR_ID:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Provider card is not owned")
        profile = ProviderProfile(
            "profile-1",
            AUTHOR_ID,
            "Scripted coeditor",
            "https://provider.example/v1/chat/completions",
            "test-model",
            "chat-completions-compatible",
            1,
            True,
        )
        return ProviderCapture(profile, SecretInput("TEST-ONLY-KEY"))


def services_for(draft: CampaignDraft, provider: ScriptedTestProvider):
    repository = MemoryCreativeWritingRepository(draft)
    services = Services(
        accounts=StubAccounts(),
        world=WorldService(repository),
        editor=EditorService(repository, repository),
        knowledge=None,
        gameplay=None,
        profiles=StubProfiles(),
        ai=AIService(provider),
        updates=None,
        reporting=None,
        queries=None,
    )
    return services, repository


class CreativeWritingScenarioTests(unittest.TestCase):
    def _complete(self, gateway, context, command, request_id):
        receipt = gateway.submit(context, command, request_id)
        if isinstance(gateway, EventGateway):
            for _ in range(10):
                if receipt.status not in {OperationStatus.PENDING, OperationStatus.RUNNING}:
                    break
                self.assertTrue(gateway.bus.dispatch_one())
                receipt = gateway.poll(context, receipt.operation_id)
        self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
        return receipt.result

    def test_reviewed_ai_edit_is_equivalent_and_requires_explicit_save(self):
        """A1/A3 slice: propose, inspect, accept into buffer, then save in both styles."""
        for architecture in ("layered", "event_based"):
            with self.subTest(architecture=architecture):
                draft = fixture()
                candidate = improved_content(draft)
                response = ProviderResponse(
                    dump_json(EditorCompletion(candidate, "Made the great hall more atmospheric."))
                )
                provider = ScriptedTestProvider((response,))
                services, repository = services_for(draft, provider)
                if architecture == "layered":
                    gateway = LayeredGateway(LayeredApplication(services))
                else:
                    gateway = EventGateway(services, start_worker=False)
                context = RequestContext("creative-writing-client", AUTHOR_ID, "creative-writing")
                buffer = EditorBuffer(draft.id, draft.revision, 0, draft.content)
                try:
                    proposal = self._complete(
                        gateway,
                        context,
                        RequestEditorProposal(
                            buffer,
                            "Make the great hall description more atmospheric.",
                            ("great-hall-description",),
                        ),
                        "proposal-request",
                    )
                    self.assertEqual(provider.calls, 1)
                    self.assertEqual(repository.draft, draft)
                    self.assertEqual(len(proposal.changes), 1)
                    self.assertEqual(proposal.changes[0].record_id, "great-hall-description")
                    self.assertTrue(proposal.changes[0].path.endswith(".text"))

                    accepted = self._complete(
                        gateway,
                        context,
                        ReviewEditorProposal(proposal.id, buffer, True),
                        "accept-proposal",
                    )
                    self.assertEqual(accepted.local_revision, 1)
                    self.assertEqual(accepted.content, candidate)
                    self.assertEqual(repository.draft, draft)
                    self.assertEqual(
                        repository.load_editor_proposal(AUTHOR_ID, proposal.id).status,
                        "accepted",
                    )

                    saved = self._complete(
                        gateway,
                        context,
                        SaveDraft(draft.id, accepted.persisted_revision, accepted.content),
                        "save-accepted-buffer",
                    )
                    self.assertEqual(saved.revision, 1)
                    self.assertEqual(saved.content, candidate)
                    self.assertEqual(provider.calls, 1)
                    if architecture == "event_based":
                        editor_topics = [entry[0] for entry in gateway.bus.trace[:3]]
                        self.assertEqual(
                            editor_topics,
                            [
                                "editor.proposal_requested",
                                "editor.context_prepared",
                                "editor.candidate_proposed",
                            ],
                        )
                finally:
                    gateway.close()

    def test_provider_change_outside_selected_record_is_rejected(self):
        draft = fixture()
        location_candidate = improved_content(draft)
        invalid_candidate = replace(location_candidate, title="An unselected title change")
        provider = ScriptedTestProvider(
            (
                ProviderResponse(
                    dump_json(EditorCompletion(invalid_candidate, "Changed more than requested."))
                ),
            )
        )
        services, repository = services_for(draft, provider)
        gateway = LayeredGateway(LayeredApplication(services))
        context = RequestContext("creative-writing-client", AUTHOR_ID, "creative-writing")
        buffer = EditorBuffer(draft.id, draft.revision, 0, draft.content)
        try:
            receipt = gateway.submit(
                context,
                RequestEditorProposal(
                    buffer,
                    "Improve only the great hall description.",
                    ("great-hall-description",),
                ),
                "out-of-scope-proposal",
            )
            self.assertEqual(receipt.status, OperationStatus.FAILED)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(repository.proposals, {})
            self.assertEqual(repository.draft, draft)
        finally:
            gateway.close()


if __name__ == "__main__":
    unittest.main()
