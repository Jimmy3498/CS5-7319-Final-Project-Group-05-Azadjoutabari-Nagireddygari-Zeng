import streamlit as st

from shared.commands import StartSession
from shared.models.ai import PlayerView
from shared.queries import Query, QueryKind

from .common import UIContext, consume_completed, current_account, query, submit


def catalog(ui: UIContext) -> None:
    st.header("Browse campaigns")
    entries = query(ui, Query(QueryKind.CATALOG))
    if entries is None:
        return

    search = st.text_input("Filter campaign titles").strip().casefold()
    visible = [entry for entry in entries if search in entry.title.casefold()]
    if not visible:
        st.info("No published campaigns match this filter.")
        return

    signed_in = current_account(ui)
    for entry in visible:
        with st.container(border=True):
            st.subheader(entry.title)
            st.write(entry.summary)
            st.caption(f"Version {entry.version}")
            options = list(zip(entry.playable_character_ids, entry.playable_character_names))
            if not options:
                st.warning("This campaign has no playable character option.")
                continue
            selected = st.selectbox(
                "Preset character",
                options,
                format_func=lambda option: option[1],
                key=f"catalog.character.{entry.published_version_id}",
            )
            if st.button(
                "Start playthrough",
                key=f"catalog.start.{entry.published_version_id}",
                disabled=signed_in is None,
                type="primary",
            ):
                submit(
                    ui,
                    f"catalog.start.{entry.published_version_id}",
                    StartSession(entry.published_version_id, selected[0]),
                )
            result = consume_completed(ui, f"catalog.start.{entry.published_version_id}")
            if isinstance(result, PlayerView):
                st.session_state["ui.play.view"] = result
                st.session_state["ui.play.session_id"] = result.session_id
                st.session_state["page"] = "My Sessions / Play"
                st.rerun()
            if signed_in is None:
                st.caption("Sign in to start a playthrough.")
