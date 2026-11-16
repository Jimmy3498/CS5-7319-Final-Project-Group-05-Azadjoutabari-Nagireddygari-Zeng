"""Private workflow context port. Do not broadcast full frames in messages."""

from dataclasses import dataclass, replace
from threading import Lock
from uuid import uuid4

from shared.commands import SubmitTurn
from shared.errors import ApplicationError, ErrorCode
from shared.models.ai import PlayerView, ProviderCapture
from shared.models.common import RequestContext
from shared.models.gameplay import AcceptedTurn, SessionSnapshot, TurnCommit, TurnProposal
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
    owner_id: str
    context: RequestContext
    command: SubmitTurn
    baseline: SessionSnapshot
    capture: ProviderCapture
    history: tuple[AcceptedTurn, ...] = ()
    proposal: TurnProposal | None = None
    candidate: SessionSnapshot | None = None
    view: PlayerView | None = None
    narration: str | None = None
    commit: TurnCommit | None = None
    stage: str = "prepared"


@dataclass(frozen=True)
class NarrationWork:
    owner_id: str
    context: RequestContext
    command: SubmitTurn
    candidate: SessionSnapshot
    view: PlayerView
    capture: ProviderCapture
    narration: str | None = None
    stage: str = "prepared"


@dataclass(frozen=True)
class FailureWork:
    context: RequestContext
    owner_id: str | None
    session_id: str | None
    public_code: str
    public_message: str


class WorkflowStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._editor: dict[str, EditorWork] = {}
        self._editor_operations: dict[str, str] = {}
        self._turns: dict[str, TurnWork] = {}
        self._narrations: dict[str, NarrationWork] = {}
        self._failures: dict[str, FailureWork] = {}
        self._turn_operations: dict[str, str] = {}

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
        """Retain bounded owner-scoped turn state and return an opaque reference."""
        with self._lock:
            if operation_id in self._turn_operations:
                return self._turn_operations[operation_id]
            reference = str(uuid4())
            self._turns[reference] = work
            self._turn_operations[operation_id] = reference
            return reference

    def private_turn(
        self, reference: str | None, context: RequestContext, expected_stage: str
    ) -> TurnWork:
        with self._lock:
            work = self._turns.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Turn workflow is no longer active")
            self._authorize(work.context, context, work.owner_id)
            if work.stage != expected_stage:
                raise ApplicationError(ErrorCode.CONFLICT, "Turn workflow stage is stale")
            return work

    def update_turn(
        self,
        reference: str | None,
        context: RequestContext,
        expected_stage: str,
        **changes,
    ) -> TurnWork:
        with self._lock:
            work = self._turns.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Turn workflow is no longer active")
            self._authorize(work.context, context, work.owner_id)
            if work.stage != expected_stage:
                raise ApplicationError(ErrorCode.CONFLICT, "Turn workflow stage is stale")
            updated = replace(work, **changes)
            self._turns[reference or ""] = updated
            return updated

    def retain_narration(
        self, reference: str | None, context: RequestContext, work: NarrationWork
    ) -> None:
        with self._lock:
            turn = self._turns.get(reference or "")
            if turn is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Turn workflow is no longer active")
            self._authorize(turn.context, context, turn.owner_id)
            self._narrations[reference or ""] = work

    def narration(
        self, reference: str | None, context: RequestContext, expected_stage: str
    ) -> NarrationWork:
        with self._lock:
            work = self._narrations.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Narration workflow is no longer active")
            self._authorize(work.context, context, work.owner_id)
            if work.stage != expected_stage:
                raise ApplicationError(ErrorCode.CONFLICT, "Narration workflow stage is stale")
            return work

    def update_narration(
        self,
        reference: str | None,
        context: RequestContext,
        expected_stage: str,
        **changes,
    ) -> NarrationWork:
        with self._lock:
            work = self._narrations.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Narration workflow is no longer active")
            self._authorize(work.context, context, work.owner_id)
            if work.stage != expected_stage:
                raise ApplicationError(ErrorCode.CONFLICT, "Narration workflow stage is stale")
            updated = replace(work, **changes)
            self._narrations[reference or ""] = updated
            return updated

    def retain_failure(self, operation_id: str, work: FailureWork) -> str:
        with self._lock:
            reference = self._turn_operations.get(operation_id) or str(uuid4())
            self._failures[reference] = work
            self._turn_operations.setdefault(operation_id, reference)
            return reference

    def failure(self, reference: str | None, context: RequestContext) -> FailureWork:
        with self._lock:
            work = self._failures.get(reference or "")
            if work is None:
                raise ApplicationError(ErrorCode.CONFLICT, "Failure workflow is no longer active")
            self._authorize(work.context, context, work.owner_id)
            return work

    def turn_for_operation(self, operation_id: str) -> TurnWork | None:
        with self._lock:
            reference = self._turn_operations.get(operation_id)
            return self._turns.get(reference or "")

    @staticmethod
    def _authorize(stored: RequestContext, supplied: RequestContext, owner_id: str | None) -> None:
        if stored.client_session_id != supplied.client_session_id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Workflow is not in this client scope")
        if supplied.user_id is not None and owner_id is not None and supplied.user_id != owner_id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Workflow is not owned by this account")

    def release(self, operation_id: str) -> None:
        """Remove terminal private state without retaining provider credentials."""
        with self._lock:
            reference = self._editor_operations.pop(operation_id, None)
            if reference is not None:
                self._editor.pop(reference, None)
            reference = self._turn_operations.pop(operation_id, None)
            if reference is not None:
                self._turns.pop(reference, None)
                self._narrations.pop(reference, None)
                self._failures.pop(reference, None)
