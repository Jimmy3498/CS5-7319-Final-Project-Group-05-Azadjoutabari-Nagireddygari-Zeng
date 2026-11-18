import streamlit as st

from shared.models.reporting import CampaignReport
from shared.queries import Query, QueryKind

from .common import UIContext, query, require_account


def reports(ui: UIContext) -> None:
    st.header("Activity & milestones")
    if require_account(ui) is None:
        return
    campaigns = query(ui, Query(QueryKind.MY_CAMPAIGNS))
    if not campaigns:
        st.info("Create a campaign to view its activity.")
        return
    campaign_id = st.selectbox(
        "Campaign",
        [campaign.id for campaign in campaigns],
        format_func=lambda identity: next(
            campaign.title for campaign in campaigns if campaign.id == identity
        ),
        key="ui.reports.campaign",
    )
    selected_campaign = next(campaign for campaign in campaigns if campaign.id == campaign_id)
    scopes = ["All published versions"]
    if selected_campaign.published_version_id:
        scopes.append("Latest published version")
    scope = st.selectbox("Report scope", scopes, key="ui.reports.scope")
    version_id = (
        selected_campaign.published_version_id if scope == "Latest published version" else None
    )
    report = query(ui, Query(QueryKind.CAMPAIGN_REPORT, campaign_id, version_id))
    if not isinstance(report, CampaignReport):
        return
    columns = st.columns(4)
    columns[0].metric("Playthroughs", report.started_playthroughs)
    columns[1].metric("Accepted turns", report.accepted_turns)
    columns[2].metric("Rejected turns", report.rejected_turns)
    columns[3].metric("Failed turns", report.failed_turns)
    if report.last_activity:
        st.caption(f"Last activity: {report.last_activity}")
    if report.projection_as_of:
        st.caption(f"Milestone projection current through: {report.projection_as_of}")
    if not report.milestones:
        st.info("No milestones are defined in the latest published version.")
        return
    st.dataframe(
        [
            {
                "Milestone": marker.label,
                "Playthroughs reached": marker.playthrough_count,
                "Eligible playthroughs": marker.eligible_playthroughs,
                "Accounts reached": marker.distinct_account_count,
                "Eligible accounts": marker.eligible_accounts,
            }
            for marker in report.milestones
        ],
        hide_index=True,
        width="stretch",
    )
