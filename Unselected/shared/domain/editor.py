from dataclasses import fields, is_dataclass, replace
from uuid import uuid4

from ..codec import dump_json
from ..commands import RequestEditorProposal, ReviewEditorProposal
from ..errors import ApplicationError, ErrorCode
from ..models.world import CampaignContent, EditorBuffer, EditorChange, EditorProposal
from ..ports import CampaignRepository, EditorProposalRepository


def _record_ids(value: object) -> tuple[str, ...]:
    found: list[str] = []

    def visit(item: object) -> None:
        if is_dataclass(item):
            identity = getattr(item, "id", None)
            if isinstance(identity, str):
                found.append(identity)
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    if len(found) != len(set(found)):
        raise ApplicationError(ErrorCode.INVALID, "Campaign record IDs must be unique")
    return tuple(found)


def _display(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is str:
        return value
    try:
        return dump_json(value)
    except TypeError:
        return str(value)


def _editor_changes(before: object, after: object) -> tuple[EditorChange, ...]:
    changes: list[EditorChange] = []

    def compare(left: object, right: object, path: str, owner: str) -> None:
        if type(left) is not type(right):
            changes.append(EditorChange(owner, path, _display(left), _display(right)))
            return
        if is_dataclass(left):
            current_owner = getattr(left, "id", owner)
            for field in fields(left):
                child_path = f"{path}.{field.name}" if path else field.name
                compare(
                    getattr(left, field.name),
                    getattr(right, field.name),
                    child_path,
                    current_owner,
                )
            return
        if isinstance(left, tuple):
            if len(left) != len(right):
                changes.append(EditorChange(owner, path, _display(left), _display(right)))
                return
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                label = getattr(left_item, "id", index)
                compare(left_item, right_item, f"{path}[{label}]", owner)
            return
        if left != right:
            changes.append(EditorChange(owner, path, _display(left), _display(right)))

    compare(before, after, "", "campaign")
    return tuple(changes)


class EditorService:
    def __init__(
        self,
        campaigns: CampaignRepository,
        proposals: EditorProposalRepository | None = None,
    ) -> None:
        self.campaigns = campaigns
        self.proposals = proposals or campaigns  # type: ignore[assignment]

    def prepare(self, owner_id: str, command: RequestEditorProposal) -> EditorBuffer:
        """Authorize and capture the exact unsaved buffer used by the provider request."""
        buffer = command.buffer
        draft = self.campaigns.load_draft(owner_id, buffer.campaign_id)
        if draft.revision != buffer.persisted_revision:
            raise ApplicationError(ErrorCode.CONFLICT, "The persisted draft revision changed")
        if buffer.local_revision < 0:
            raise ApplicationError(ErrorCode.INVALID, "Invalid editor buffer revision")
        known_ids = set(_record_ids(buffer.content)) | {"campaign"}
        if not command.selected_record_ids:
            raise ApplicationError(ErrorCode.INVALID, "Select at least one editor record")
        if len(command.selected_record_ids) > 128 or len(set(command.selected_record_ids)) != len(
            command.selected_record_ids
        ):
            raise ApplicationError(ErrorCode.INVALID, "Invalid selected editor records")
        unknown = set(command.selected_record_ids) - known_ids
        if unknown:
            raise ApplicationError(ErrorCode.INVALID, "Selected editor record does not exist")
        if not command.instruction.strip() or len(command.instruction) > 8_000:
            raise ApplicationError(ErrorCode.INVALID, "Editor instruction is required and bounded")
        return buffer

    def retain_proposal(
        self,
        owner_id: str,
        buffer: EditorBuffer,
        candidate: CampaignContent,
        summary: str,
        selected_ids: tuple[str, ...],
    ) -> EditorProposal:
        """Validate scope and retain a candidate for explicit human review."""
        if not isinstance(candidate, CampaignContent):
            raise ApplicationError(ErrorCode.PROVIDER, "Provider candidate has an invalid shape")
        if len(dump_json(candidate).encode("utf-8")) > 1024 * 1024:
            raise ApplicationError(ErrorCode.PROVIDER, "Provider candidate exceeds the size limit")
        original_ids = set(_record_ids(buffer.content))
        candidate_ids = set(_record_ids(candidate))
        if original_ids != candidate_ids:
            raise ApplicationError(ErrorCode.PROVIDER, "Provider changed stable campaign IDs")
        changes = _editor_changes(buffer.content, candidate)
        changed_records = {change.record_id for change in changes}
        if changed_records - set(selected_ids):
            raise ApplicationError(
                ErrorCode.PROVIDER, "Provider changed content outside the selected records"
            )
        proposal = EditorProposal(
            str(uuid4()),
            buffer.campaign_id,
            buffer.persisted_revision,
            buffer.local_revision,
            summary,
            candidate,
            changes,
        )
        self.proposals.save_editor_proposal(owner_id, proposal)
        return proposal

    def review(self, owner_id: str, command: ReviewEditorProposal) -> EditorBuffer:
        """Resolve a current proposal; acceptance changes only the caller's buffer."""
        proposal = self.proposals.load_editor_proposal(owner_id, command.proposal_id)
        buffer = command.buffer
        draft = self.campaigns.load_draft(owner_id, buffer.campaign_id)
        if proposal.status != "pending":
            raise ApplicationError(ErrorCode.CONFLICT, "Editor proposal was already reviewed")
        if proposal.campaign_id != buffer.campaign_id:
            raise ApplicationError(ErrorCode.INVALID, "Proposal and editor buffer do not match")
        if (
            proposal.base_persisted_revision != buffer.persisted_revision
            or proposal.base_buffer_revision != buffer.local_revision
            or draft.revision != buffer.persisted_revision
        ):
            raise ApplicationError(ErrorCode.CONFLICT, "Editor proposal is stale")
        status = "accepted" if command.accept else "rejected"
        self.proposals.resolve_editor_proposal(owner_id, proposal.id, status)
        if not command.accept:
            return buffer
        return replace(buffer, local_revision=buffer.local_revision + 1, content=proposal.candidate)

    def edit_record(self, buffer: EditorBuffer, candidate: CampaignContent) -> EditorBuffer:
        """Apply a typed manual edit locally; SaveDraft remains the persistence boundary."""
        if not isinstance(candidate, CampaignContent):
            raise ApplicationError(ErrorCode.INVALID, "Invalid campaign content")
        return replace(buffer, local_revision=buffer.local_revision + 1, content=candidate)
