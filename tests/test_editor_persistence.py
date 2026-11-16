import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.codec import decode, load_json
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.world import CampaignDraft, EditorChange, EditorProposal


class EditorProposalPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteStore(Path(temporary.name) / "editor.sqlite3")
        self.store.initialize()
        self.owner = self.store.create_account("Author", "test-hash").id
        fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        fixture = decode(
            load_json(fixture_path.read_text(encoding="utf-8"))["draft"], CampaignDraft
        )
        self.draft = self.store.create_campaign(self.owner, "Draft", "Summary")
        self.draft = self.store.save_draft(
            self.owner,
            self.draft.id,
            self.draft.revision,
            fixture.content,
        )

    def test_save_load_and_resolve_proposal(self):
        candidate = replace(self.draft.content, title="Improved title")
        proposal = EditorProposal(
            "proposal-1",
            self.draft.id,
            self.draft.revision,
            2,
            "Improved the title.",
            candidate,
            (EditorChange("campaign", "title", self.draft.content.title, candidate.title),),
        )
        self.store.save_editor_proposal(self.owner, proposal)
        self.assertEqual(self.store.load_editor_proposal(self.owner, proposal.id), proposal)
        accepted = self.store.resolve_editor_proposal(self.owner, proposal.id, "accepted")
        self.assertEqual(accepted.status, "accepted")
        with self.assertRaises(ApplicationError) as caught:
            self.store.resolve_editor_proposal(self.owner, proposal.id, "rejected")
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)

    def test_proposals_are_owner_scoped(self):
        other = self.store.create_account("Other", "test-hash").id
        proposal = EditorProposal(
            "proposal-2",
            self.draft.id,
            self.draft.revision,
            0,
            "No changes.",
            self.draft.content,
        )
        self.store.save_editor_proposal(self.owner, proposal)
        with self.assertRaises(ApplicationError) as caught:
            self.store.load_editor_proposal(other, proposal.id)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
