from ..models.gameplay import CommittedTurnNotice
from ..models.reporting import CampaignReport
from ..ports import ReportRepository, SessionRepository


class ReportingService:
    def __init__(self, repository: ReportRepository, sessions: SessionRepository) -> None:
        self.repository = repository
        self.sessions = sessions

    def consume_committed_turn(self, notice: CommittedTurnNotice) -> None:
        self.repository.apply_committed_notice(notice)
        self.sessions.acknowledge_notification("reporting", notice.event_id)

    def report(self, owner_id: str, campaign_id: str, version_id: str | None) -> CampaignReport:
        return self.repository.campaign_report(owner_id, campaign_id, version_id)

    def catch_up(self) -> None:
        for notice in self.sessions.pending_notifications():
            self.consume_committed_turn(notice)
