import streamlit as st

from shared.commands import AdoptCampaignUpdate, RetryTurn, SubmitTurn
from shared.gateway import OperationReceipt, OperationStatus
from shared.models.ai import PlayerView
from shared.models.views import TurnResultView, UpdatePreviewView
from shared.queries import Query, QueryKind

from .common import UIContext, consume_completed, query, require_account, submit


def _render_view(view: PlayerView) -> None:
    st.caption(f"Session revision {view.revision} · Character {view.player_character_id}")
    if view.accepted_history:
        st.subheader("Story so far")
        for index, narration in enumerate(view.accepted_history, start=1):
            with st.chat_message("assistant"):
                st.markdown(narration)
                st.caption(f"Turn {index}")
    if view.facts:
        with st.expander("What your character knows"):
            for fact in view.facts:
                st.markdown(f"- **{fact.stance}:** {fact.text}")


def play(ui: UIContext) -> None:
    st.header("My playthroughs")
    if require_account(ui) is None:
        return

    sessions = query(ui, Query(QueryKind.MY_SESSIONS))
    if sessions is None:
        return
    if not sessions:
        st.info("Start a published campaign from Browse Campaigns.")
        return

    selected_id = st.selectbox(
        "Saved session",
        [session.id for session in sessions],
        format_func=lambda identity: next(
            f"{session.campaign_title} · revision {session.revision}"
            for session in sessions
            if session.id == identity
        ),
        key="ui.play.session_id",
    )
    view = query(ui, Query(QueryKind.RESUME_SESSION, selected_id))
    if not isinstance(view, PlayerView):
        return
    st.session_state["ui.play.view"] = view
    _render_view(view)

    st.subheader("Your next action")
    with st.form("turn_form", clear_on_submit=True):
        player_input = st.text_area("What do you do or say?", max_chars=8000)
        send = st.form_submit_button("Send action", type="primary")
    if send:
        submit(ui, "play.turn", SubmitTurn(view.session_id, view.revision, player_input))
    result = consume_completed(ui, "play.turn")
    if isinstance(result, TurnResultView):
        if result.reporting_pending:
            st.warning("The turn was saved, but activity reporting is still pending.")
        refreshed = query(ui, Query(QueryKind.RESUME_SESSION, view.session_id))
        if isinstance(refreshed, PlayerView):
            st.session_state["ui.play.view"] = refreshed
            st.rerun()

    receipt = st.session_state.get("ui.play.turn.receipt")
    if isinstance(receipt, OperationReceipt) and receipt.status in {
        OperationStatus.REJECTED,
        OperationStatus.FAILED,
    }:
        st.caption("Retry reuses the prior private player input in a new linked operation.")
        if st.button("Retry failed action", icon=":material/replay:"):
            submit(
                ui,
                "play.retry",
                RetryTurn(receipt.operation_id, view.session_id, view.revision),
            )
    retried = consume_completed(ui, "play.retry")
    if isinstance(retried, TurnResultView):
        st.rerun()

    selected = next(session for session in sessions if session.id == selected_id)
    if selected.update_available and selected.latest_version_id:
        st.divider()
        st.subheader("Campaign update")
        preview = query(
            ui,
            Query(QueryKind.UPDATE_PREVIEW, selected.id, selected.latest_version_id),
        )
        if isinstance(preview, UpdatePreviewView):
            if preview.can_adopt:
                st.success("The latest campaign version is compatible with this playthrough.")
                if st.button("Adopt latest version", icon=":material/upgrade:"):
                    submit(
                        ui,
                        "play.update",
                        AdoptCampaignUpdate(
                            selected.id, selected.latest_version_id, selected.revision
                        ),
                    )
            else:
                st.warning(
                    "This update cannot be adopted safely: " + ", ".join(preview.conflict_codes)
                )
        updated = consume_completed(ui, "play.update")
        if isinstance(updated, PlayerView):
            st.rerun()
