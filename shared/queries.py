from dataclasses import dataclass
from enum import StrEnum

from .models.accounts import AccountView
from .models.ai import PlayerView, ProviderProfile
from .models.common import Issue
from .models.gameplay import SessionSummary
from .models.reporting import CampaignReport
from .models.views import CampaignSummary, UpdatePreviewView
from .models.world import CampaignDraft, CatalogEntry


class QueryKind(StrEnum):
    CURRENT_ACCOUNT = "current_account"
    CATALOG = "catalog"
    MY_CAMPAIGNS = "my_campaigns"
    EDITOR_DRAFT = "editor_draft"
    MY_SESSIONS = "my_sessions"
    RESUME_SESSION = "resume_session"
    CAMPAIGN_REPORT = "campaign_report"
    PROVIDER_CARDS = "provider_cards"
    ACTIVE_PROVIDER_CARD = "active_provider_card"
    UPDATE_PREVIEW = "update_preview"
    CAMPAIGN_READINESS = "campaign_readiness"


@dataclass(frozen=True)
class Query:
    kind: QueryKind
    resource_id: str | None = None
    version_id: str | None = None


# Closed query result families; implementation still performs caller authorization/projection.
QueryResult = (
    AccountView
    | tuple[CatalogEntry, ...]
    | tuple[CampaignSummary, ...]
    | CampaignDraft
    | tuple[SessionSummary, ...]
    | PlayerView
    | CampaignReport
    | tuple[ProviderProfile, ...]
    | ProviderProfile
    | UpdatePreviewView
    | tuple[Issue, ...]
)
