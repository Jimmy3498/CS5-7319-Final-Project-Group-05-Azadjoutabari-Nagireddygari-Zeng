from ..errors import ApplicationError, ErrorCode, unfinished
from ..models.common import RequestContext
from ..ports import CampaignRepository, ProfileRepository, SessionRepository
from ..queries import Query, QueryKind, QueryResult
from .accounts import AccountService
from .knowledge import KnowledgePolicy
from .reporting import ReportingService
from .updates import UpdateService


class QueryService:
    def __init__(
        self,
        accounts: AccountService,
        campaigns: CampaignRepository,
        sessions: SessionRepository,
        profiles: ProfileRepository,
        reporting: ReportingService,
        updates: UpdateService,
        knowledge: KnowledgePolicy,
    ) -> None:
        self.accounts, self.campaigns, self.sessions = accounts, campaigns, sessions
        self.profiles, self.reporting, self.updates = profiles, reporting, updates
        self.knowledge = knowledge

    def read(self, context: RequestContext, query: Query) -> QueryResult:
        if query.kind == QueryKind.CATALOG:
            return self.campaigns.catalog()

        account = self.accounts.require_user(context)

        if query.kind == QueryKind.MY_CAMPAIGNS:
            return self.campaigns.drafts_for_owner(account.id)

        if query.kind == QueryKind.EDITOR_DRAFT:
            if not query.resource_id:
                raise ApplicationError(ErrorCode.INVALID, "Campaign ID is required")
            return self.campaigns.load_draft(account.id, query.resource_id)

        if query.kind == QueryKind.MY_SESSIONS:
            return self.sessions.sessions_for_owner(account.id)

        if query.kind == QueryKind.RESUME_SESSION:
            if not query.resource_id:
                raise ApplicationError(ErrorCode.INVALID, "Session ID is required")
            session = self.sessions.load_session(account.id, query.resource_id)
            history = self.sessions.accepted_history(account.id, session.id)
            return self.knowledge.player_view(session, history, "")

        if query.kind == QueryKind.PROVIDER_CARDS:
            return self.profiles.list_profiles(account.id)

        unfinished("PERSISTENCE")
