from ..codec import decode, dump_json, load_json
from ..errors import ApplicationError, ErrorCode, unfinished
from ..models.ai import (
    DirectorContext,
    EditorCompletion,
    PlayerView,
    ProviderCapture,
    ProviderMessage,
)
from ..models.gameplay import TurnProposal
from ..models.world import CampaignContent, EditorBuffer
from ..ports import ProviderTransport


class AIService:
    def __init__(self, transport: ProviderTransport) -> None:
        self.transport = transport

    def propose_editor(
        self,
        buffer: EditorBuffer,
        instruction: str,
        selected_ids: tuple[str, ...],
        capture: ProviderCapture,
    ) -> tuple[CampaignContent, str]:
        """Return a typed candidate without mutating the draft or editor buffer."""
        if not instruction.strip() or len(instruction) > 8_000:
            raise ApplicationError(ErrorCode.INVALID, "Editor instruction is required and bounded")
        if not selected_ids or len(selected_ids) > 128:
            raise ApplicationError(ErrorCode.INVALID, "Select at least one bounded editor record")
        if len(set(selected_ids)) != len(selected_ids):
            raise ApplicationError(ErrorCode.INVALID, "Selected editor records must be unique")

        system = ProviderMessage(
            "system",
            "You are a campaign coeditor. Return JSON only with exactly two fields: "
            "candidate (the complete CampaignContent object) and summary (a short description). "
            "Preserve every stable ID and change only the selected record IDs. Do not add fields, "
            "execute tools, publish, save, or include Markdown.",
        )
        request = ProviderMessage(
            "user",
            dump_json(
                {
                    "campaign_id": buffer.campaign_id,
                    "persisted_revision": buffer.persisted_revision,
                    "buffer_revision": buffer.local_revision,
                    "instruction": instruction.strip(),
                    "selected_record_ids": selected_ids,
                    "content": buffer.content,
                }
            ),
        )
        response = self.transport.complete((system, request), capture)
        try:
            completion = decode(load_json(response.content), EditorCompletion)
        except (TypeError, ValueError) as error:
            raise ApplicationError(
                ErrorCode.PROVIDER, "Provider returned an invalid editor proposal"
            ) from error
        summary = completion.summary.strip()
        if not summary or len(summary) > 4_000:
            raise ApplicationError(ErrorCode.PROVIDER, "Provider returned an invalid summary")
        return completion.candidate, summary

    def plan_turn(self, context: DirectorContext, capture: ProviderCapture) -> TurnProposal:
        """TODO PROVIDER: bounded structured effects and fact IDs; no unrestricted free-text
        'publicProjection' handed to narrator. Unknown fields/types reject; no paid auto-retry.
        """
        unfinished("PROVIDER")

    def narrate(self, view: PlayerView, capture: ProviderCapture) -> str:
        """TODO PROVIDER: render PlayerView in its own context/history.
        Exclude DirectorContext, raw planner text and privileged snapshots.
        """
        unfinished("PROVIDER")
