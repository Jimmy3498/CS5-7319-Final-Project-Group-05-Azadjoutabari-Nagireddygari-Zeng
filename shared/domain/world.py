from ..codec import dump_json
from ..commands import CreateCampaign, PublishCampaign, SaveDraft
from ..errors import ApplicationError, ErrorCode, unfinished
from ..models.common import Issue
from ..models.world import CampaignContent, CampaignDraft, CatalogEntry
from ..ports import CampaignRepository


class WorldService:
    def __init__(self, repository: CampaignRepository) -> None:
        self.repository = repository

    def create(self, owner_id: str, command: CreateCampaign) -> CampaignDraft:
        """TODO WORLD: create a private incomplete draft. Creation is not publication."""
        unfinished("WORLD")

    def save(self, owner_id: str, command: SaveDraft) -> CampaignDraft:
        """Save typed content with optimistic concurrency; incomplete drafts are allowed."""
        if not isinstance(command.content, CampaignContent):
            raise ApplicationError(ErrorCode.INVALID, "Invalid campaign draft content")
        if command.expected_revision < 0:
            raise ApplicationError(ErrorCode.INVALID, "Invalid expected draft revision")
        if len(dump_json(command.content).encode("utf-8")) > 1024 * 1024:
            raise ApplicationError(ErrorCode.INVALID, "Campaign draft exceeds the size limit")
        return self.repository.save_draft(
            owner_id,
            command.campaign_id,
            command.expected_revision,
            command.content,
        )

    def readiness(self, content: CampaignContent) -> tuple[Issue, ...]:
        """TODO WORLD: setup, preset PC/start, referential/state validity, explicit access,
        valid closed conditions/stat bands/templates. No AI quality-score gate.
        """
        unfinished("WORLD")

    def publish(self, owner_id: str, command: PublishCampaign) -> CatalogEntry:
        """TODO WORLD: load owned draft, readiness, atomic immutable published revision.
        Existing sessions remain pinned; return public catalog DTO only.
        """
        unfinished("WORLD")
