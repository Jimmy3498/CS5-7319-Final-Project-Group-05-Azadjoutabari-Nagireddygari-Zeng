"""Private workflow context port. Do not broadcast full frames in messages."""

from dataclasses import dataclass, replace
from threading import Lock
from uuid import uuid4

from shared.commands import SubmitTurn
from shared.errors import ApplicationError, ErrorCode, unfinished
from shared.models.ai import PlayerView, ProviderCapture
from shared.models.common import RequestContext
from shared.models.gameplay import SessionSnapshot, TurnProposal
from shared.models.world import CampaignContent, EditorBuffer


@dataclass(frozen=True)
class EditorWork:
    owner_id: str
    context: RequestContext
    buffer: EditorBuffer
    instruction: str
    selected_ids: tuple[str, ...]
    capture: ProviderCapture
    candidate: CampaignContent | None = None
    summary: str | None = None
    stage: str = "prepared"


@dataclass(frozen=True)
class TurnWork:
    context: RequestContext
    command: SubmitTurn
    baseline: SessionSnapshot
    capture: ProviderCapture
    proposal: TurnProposal | None = None
    candidate: SessionSnapshot | None = None


@dataclass(frozen=True)
class NarrationWork:
    view: PlayerView
    capture: ProviderCapture


class WorkflowStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._editor: dict[str, EditorWork] = {}
        self._editor_operations: dict[str, str] = {}

    def retain_editor(self, operation_id: str, work: EditorWork) -> str:
        """Retain private editor data and return an opaque continuation reference."""
        with self._lock:
            if operation_id in self._editor_operations:
                return self._editor_operations[operation_id]
            reference = str(uuid4())
            self._editor[reference] = work
            self._editor_operations[operation_id] = reference
            return reference

    def private_editor(
        self, reference: str | None, context: RequestContext, expected_stage: str
    ) -> EditorWork:
        with self._lock:
            work = self._editor.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Editor workflow is no longer active")
            if work.context.client_session_id != context.client_session_id:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Editor workflow is not in this scope")
            if context.user_id is not None and context.user_id != work.owner_id:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Editor workflow is not owned by user")
            if work.stage != expected_stage:
                raise ApplicationError(ErrorCode.CONFLICT, "Editor workflow stage is stale")
            return work

    def retain_editor_candidate(
        self,
        reference: str | None,
        context: RequestContext,
        candidate: CampaignContent,
        summary: str,
    ) -> EditorWork:
        with self._lock:
            work = self._editor.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Editor workflow is no longer active")
            if work.context.client_session_id != context.client_session_id:
                raise ApplicationError(ErrorCode.FORBIDDEN, "Editor workflow is not in this scope")
            if work.stage != "prepared":
                raise ApplicationError(ErrorCode.CONFLICT, "Editor provider result is stale")
            updated = replace(
                work,
                candidate=candidate,
                summary=summary,
                stage="candidate",
            )
            self._editor[reference or ""] = updated
            return updated

    def retain_turn(self, operation_id: str, work: TurnWork) -> str:
        """TODO EVENTS: bounded owner-scoped worker state; opaque work reference. Secrets
        stay in process/private credential storage, not durable public event payloads.
        """
        unfinished("EVENTS")

    def private_turn(self, reference: str, context: RequestContext) -> TurnWork:
        """TODO EVENTS: authorize stage and owner; for planner/validator/committer only."""
        unfinished("EVENTS")

    def narration(self, reference: str, context: RequestContext) -> NarrationWork:
        """TODO EVENTS: exact player-scoped projection, not a full TurnWork frame."""
        unfinished("EVENTS")

    def release(self, operation_id: str) -> None:
        """Remove terminal private state without retaining provider credentials."""
        with self._lock:
            reference = self._editor_operations.pop(operation_id, None)
            if reference is not None:
                self._editor.pop(reference, None)
