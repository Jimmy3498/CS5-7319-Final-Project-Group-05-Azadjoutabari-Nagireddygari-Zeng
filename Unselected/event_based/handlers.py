"""Component handlers; no shared whole-turn coordinator or inline subscriber calls.

Single-capability commands and the three-stage editor flow are implemented.
Gameplay uses staged messages and opaque private work references. See docs/ARCHITECTURE.md.
"""

from dataclasses import replace
from uuid import uuid4

from shared.commands import RequestEditorProposal, SubmitTurn
from shared.domain.services import Services
from shared.errors import ApplicationError, ErrorCode
from shared.gateway import OperationStatus
from shared.models.views import TurnResultView
from shared.operation_status import OperationStatusStore

from .bus import InProcessBus
from .messages import Message, Topic
from .state import EditorWork, FailureWork, NarrationWork, TurnWork, WorkflowStateStore


class ComponentHandlers:
    def __init__(
        self, services: Services, bus: InProcessBus, statuses: OperationStatusStore
    ) -> None:
        self.s, self.bus, self.statuses = services, bus, statuses
        self.state = WorkflowStateStore()

    def next_event(self, source: Message, topic: Topic, work_reference: str) -> None:
        """Queue continuation; do not call another handler directly or carry original command."""
        self.bus.publish(
            Message(
                str(uuid4()),
                source.operation_id,
                source.correlation_id,
                source.id,
                topic,
                source.context,
                work_reference=work_reference,
            )
        )

    def complete(self, message: Message, result: object = None) -> None:
        # The durable accepted result lives in SQLite; this is the client-session delivery receipt.
        self.statuses.update(
            message.operation_id,
            OperationStatus.COMPLETED,
            "Application operation completed",
            result,
        )

    def account(self, message: Message) -> None:
        if message.topic == Topic.REGISTER:
            result = self.s.accounts.register(message.context, message.command)
        elif message.topic == Topic.LOGIN:
            result = self.s.accounts.login(message.context, message.command)
        else:
            result = self.s.accounts.logout(message.context)
        self.complete(message, result)

    def campaign(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        action = {
            Topic.CREATE_CAMPAIGN: self.s.world.create,
            Topic.SAVE_DRAFT: self.s.world.save,
            Topic.PUBLISH: self.s.world.publish,
        }[message.topic]
        self.complete(message, action(user.id, message.command))

    def profile(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        action = {
            Topic.SAVE_CARD: self.s.profiles.save,
            Topic.ACTIVATE_CARD: self.s.profiles.activate,
            Topic.DELETE_CARD: self.s.profiles.delete,
        }[message.topic]
        self.complete(message, action(user.id, message.command))

    def start_session(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        session = self.s.gameplay.start(user.id, message.command)
        self.complete(message, self.s.knowledge.player_view(session, (), ""))

    def update_session(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        session = self.s.updates.adopt(user.id, message.operation_id, message.command)
        history = self.s.gameplay.history(user.id, session.id)
        self.complete(message, self.s.knowledge.player_view(session, history, ""))

    def review_editor(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        self.complete(message, self.s.editor.review(user.id, message.command))

    def editor_requested(self, message: Message) -> None:
        """Authorize and retain private editor context before queuing provider work."""
        if not isinstance(message.command, RequestEditorProposal):
            raise ApplicationError(ErrorCode.INVALID, "Invalid editor request")
        user = self.s.accounts.require_user(message.context)
        buffer = self.s.editor.prepare(user.id, message.command)
        capture = self.s.profiles.capture(user.id)
        reference = self.state.retain_editor(
            message.operation_id,
            EditorWork(
                user.id,
                message.context,
                buffer,
                message.command.instruction,
                message.command.selected_record_ids,
                capture,
            ),
        )
        self.statuses.update(
            message.operation_id,
            OperationStatus.RUNNING,
            "Editor context prepared",
        )
        self.next_event(message, Topic.EDITOR_CONTEXT, reference)

    def editor_model(self, message: Message) -> None:
        """Make the single paid provider call and retain its typed candidate privately."""
        work = self.state.private_editor(message.work_reference, message.context, "prepared")
        candidate, summary = self.s.ai.propose_editor(
            work.buffer,
            work.instruction,
            work.selected_ids,
            work.capture,
        )
        self.state.retain_editor_candidate(
            message.work_reference,
            message.context,
            candidate,
            summary,
        )
        self.next_event(message, Topic.EDITOR_CANDIDATE, message.work_reference or "")

    def editor_candidate(self, message: Message) -> None:
        """Persist the reviewable proposal without accepting, saving, or publishing it."""
        work = self.state.private_editor(message.work_reference, message.context, "candidate")
        if work.candidate is None or work.summary is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Editor candidate is incomplete")
        proposal = self.s.editor.retain_proposal(
            work.owner_id,
            work.buffer,
            work.candidate,
            work.summary,
            work.selected_ids,
        )
        self.complete(message, proposal)
        self.state.release(message.operation_id)

    def turn_requested(self, message: Message) -> None:
        if not isinstance(message.command, SubmitTurn):
            raise ApplicationError(ErrorCode.INVALID, "Invalid turn request")
        user = self.s.accounts.require_user(message.context)
        request_id = message.context.request_id
        if not request_id:
            raise ApplicationError(ErrorCode.INVALID, "Missing request identity")
        admission = self.s.gameplay.prepare(
            user.id, message.command, message.operation_id, request_id
        )
        if admission.previous_commit is not None:
            self.complete(message, TurnResultView(admission.previous_commit.turn, None))
            return
        if admission.baseline is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Invalid turn admission")
        try:
            capture = self.s.profiles.capture(user.id)
            history = self.s.gameplay.history(user.id, admission.baseline.id)
        except Exception as error:
            code = error.code if isinstance(error, ApplicationError) else ErrorCode.INTERNAL
            self.s.gameplay.record_failure(
                user.id,
                message.operation_id,
                message.command.session_id,
                code.value,
                code == ErrorCode.PROVIDER_UNCERTAIN,
            )
            raise
        context = replace(message.context, user_id=user.id)
        reference = self.state.retain_turn(
            message.operation_id,
            TurnWork(
                user.id,
                context,
                message.command,
                admission.baseline,
                capture,
                history,
            ),
        )
        self.statuses.update(message.operation_id, OperationStatus.RUNNING, "Turn context prepared")
        self.next_event(message, Topic.TURN_CONTEXT, reference)

    def director_model(self, message: Message) -> None:
        work = self.state.private_turn(message.work_reference, message.context, "prepared")
        context = self.s.knowledge.director_context(
            work.baseline, work.command.player_input, work.history
        )
        proposal = self.s.ai.plan_turn(context, work.capture)
        self.state.update_turn(
            message.work_reference,
            message.context,
            "prepared",
            proposal=proposal,
            stage="planned",
        )
        self.next_event(message, Topic.TURN_PLAN, message.work_reference or "")

    def validate_turn(self, message: Message) -> None:
        work = self.state.private_turn(message.work_reference, message.context, "planned")
        if work.proposal is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Turn plan is missing")
        candidate = self.s.gameplay.validate_and_project(work.baseline, work.proposal)
        candidate = self.s.knowledge.authorize_disclosures(work.baseline, candidate, work.proposal)
        view = self.s.knowledge.player_view(candidate, work.history, work.command.player_input)
        self.state.retain_narration(
            message.work_reference,
            message.context,
            NarrationWork(
                work.owner_id,
                work.context,
                work.command,
                candidate,
                view,
                work.capture,
            ),
        )
        self.state.update_turn(
            message.work_reference,
            message.context,
            "planned",
            candidate=candidate,
            view=view,
            stage="validated",
        )
        self.next_event(message, Topic.TURN_VALIDATED, message.work_reference or "")

    def narrator_model(self, message: Message) -> None:
        work = self.state.narration(message.work_reference, message.context, "prepared")
        narration = self.s.ai.narrate(work.view, work.capture)
        self.state.update_narration(
            message.work_reference,
            message.context,
            "prepared",
            narration=narration,
            stage="narrated",
        )
        self.state.update_turn(
            message.work_reference,
            message.context,
            "validated",
            narration=narration,
            stage="narrated",
        )
        self.next_event(message, Topic.NARRATION, message.work_reference or "")

    def commit_turn(self, message: Message) -> None:
        turn = self.state.private_turn(message.work_reference, message.context, "narrated")
        narration = self.state.narration(message.work_reference, message.context, "narrated")
        if turn.candidate is None or narration.narration is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Validated turn is incomplete")
        commit = self.s.gameplay.commit(
            turn.owner_id,
            message.operation_id,
            turn.command,
            turn.candidate,
            narration.narration,
        )
        self.state.update_turn(
            message.work_reference,
            message.context,
            "narrated",
            commit=commit,
            stage="committed",
        )
        notice = commit.notice
        self.bus.publish(
            Message(
                notice.event_id,
                notice.operation_id,
                message.correlation_id,
                message.id,
                Topic.TURN_COMMITTED,
                message.context,
                work_reference=message.work_reference,
                notice=notice,
            )
        )

    def result_consumer(self, message: Message) -> None:
        if message.notice is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Committed result is unavailable")
        work = self.state.turn_for_operation(message.operation_id)
        if work is not None:
            work = self.state.private_turn(message.work_reference, message.context, "committed")
            if work.commit is None:
                raise ApplicationError(ErrorCode.INTERNAL, "Committed result is unavailable")
            self.complete(message, TurnResultView(work.commit.turn, None))
        self.s.gameplay.acknowledge("result", message.notice.event_id)
        self.state.release(message.operation_id)

    def report_consumer(self, message: Message) -> None:
        if message.notice is None:
            raise ApplicationError(ErrorCode.INTERNAL, "Committed notice is missing")
        self.s.reporting.consume_committed_turn(message.notice)

    def failure_consumer(self, message: Message) -> None:
        work = self.state.failure(message.work_reference, message.context)
        if work.owner_id is not None and work.session_id is not None:
            self.s.gameplay.record_failure(
                work.owner_id,
                message.operation_id,
                work.session_id,
                work.public_code,
                work.public_code == ErrorCode.PROVIDER_UNCERTAIN.value,
            )
        try:
            code = ErrorCode(work.public_code)
        except ValueError:
            code = ErrorCode.INTERNAL
        self.statuses.fail(message.operation_id, ApplicationError(code, work.public_message))
        self.state.release(message.operation_id)

    def retry(self, message: Message) -> None:
        user = self.s.accounts.require_user(message.context)
        if message.command is None or message.context.request_id is None:
            raise ApplicationError(ErrorCode.INVALID, "Invalid retry request")
        retry = self.s.gameplay.retry(
            user.id,
            message.command,
            message.operation_id,
            message.context.request_id,
        )
        if not isinstance(retry, SubmitTurn):
            self.complete(message, TurnResultView(retry.turn, None))
            return
        self.bus.publish(
            Message(
                str(uuid4()),
                message.operation_id,
                message.correlation_id,
                message.id,
                Topic.TURN_REQUEST,
                message.context,
                command=retry,
            )
        )

    def fail_turn(self, message: Message, error: Exception) -> None:
        """Convert a private stage failure into a sanitized terminal event."""
        code = error.code if isinstance(error, ApplicationError) else ErrorCode.INTERNAL
        public_message = (
            error.public_message
            if isinstance(error, ApplicationError)
            else "Internal failure; inspect sanitized diagnostics"
        )
        turn = self.state.turn_for_operation(message.operation_id)
        owner_id = turn.owner_id if turn is not None else None
        session_id = turn.command.session_id if turn is not None else None
        if turn is None and isinstance(message.command, SubmitTurn):
            session_id = message.command.session_id
            try:
                owner_id = self.s.accounts.require_user(message.context).id
            except ApplicationError:
                owner_id = None
        reference = self.state.retain_failure(
            message.operation_id,
            FailureWork(message.context, owner_id, session_id, code.value, public_message),
        )
        self.bus.publish(
            Message(
                str(uuid4()),
                message.operation_id,
                message.correlation_id,
                message.id,
                Topic.TURN_FAILED,
                message.context,
                work_reference=reference,
            )
        )

    def register(self) -> None:
        registrations = {
            Topic.REGISTER: ("accounts", self.account),
            Topic.LOGIN: ("accounts", self.account),
            Topic.LOGOUT: ("accounts", self.account),
            Topic.CREATE_CAMPAIGN: ("campaigns", self.campaign),
            Topic.SAVE_DRAFT: ("campaigns", self.campaign),
            Topic.PUBLISH: ("campaigns", self.campaign),
            Topic.SAVE_CARD: ("profiles", self.profile),
            Topic.ACTIVATE_CARD: ("profiles", self.profile),
            Topic.DELETE_CARD: ("profiles", self.profile),
            Topic.START_SESSION: ("sessions", self.start_session),
            Topic.UPDATE: ("updates", self.update_session),
            Topic.EDITOR_REVIEW: ("editor", self.review_editor),
            Topic.EDITOR_REQUEST: ("editor-context", self.editor_requested),
            Topic.EDITOR_CONTEXT: ("editor-model", self.editor_model),
            Topic.EDITOR_CANDIDATE: ("editor-proposal", self.editor_candidate),
            Topic.TURN_REQUEST: ("gameplay-admission", self.turn_requested),
            Topic.TURN_CONTEXT: ("director-model", self.director_model),
            Topic.TURN_PLAN: ("validation", self.validate_turn),
            Topic.TURN_VALIDATED: ("narrator-model", self.narrator_model),
            Topic.NARRATION: ("persistence", self.commit_turn),
            Topic.TURN_COMMITTED: ("result-consumer", self.result_consumer),
            Topic.TURN_FAILED: ("attempt-history", self.failure_consumer),
            Topic.RETRY: ("retry-admission", self.retry),
        }
        for topic, (name, handler) in registrations.items():
            self.bus.subscribe(topic, name, handler)
        self.bus.subscribe(Topic.TURN_COMMITTED, "report-consumer", self.report_consumer)
