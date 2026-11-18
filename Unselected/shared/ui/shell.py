import streamlit as st

from shared.gateway import ApplicationGateway
from shared.models.common import RequestContext

from .activity import reports
from .catalog import catalog
from .common import UIContext
from .editor import editor
from .play import play
from .profiles import profiles

PAGES = (
    "Overview",
    "Browse Campaigns",
    "My Campaigns / Editor",
    "My Sessions / Play",
    "Activity & Milestones",
    "API Cards",
)

RENDERERS = {
    "Browse Campaigns": catalog,
    "My Campaigns / Editor": editor,
    "My Sessions / Play": play,
    "Activity & Milestones": reports,
    "API Cards": profiles,
}


def render(page: str, gateway: ApplicationGateway, context: RequestContext) -> None:
    ui = UIContext(gateway, context)
    if page == "Overview":
        st.header("Welcome to Story Studio")
        st.caption(
            "Create an interactive world, publish it for players, or continue an existing story."
        )
        create_col, browse_col, play_col = st.columns(3)
        with create_col.container(border=True, height="stretch"):
            st.subheader("Create", icon=":material/edit_note:")
            st.write("Develop campaigns and review AI-assisted suggestions.")
            st.space("stretch")
            st.button(
                "Open editor",
                key="overview.editor",
                icon=":material/arrow_forward:",
                width="stretch",
                on_click=_open_page,
                args=("My Campaigns / Editor",),
            )
        with browse_col.container(border=True, height="stretch"):
            st.subheader("Explore", icon=":material/explore:")
            st.write("Browse published campaigns and choose a playable character.")
            st.space("stretch")
            st.button(
                "Browse campaigns",
                key="overview.catalog",
                icon=":material/arrow_forward:",
                width="stretch",
                on_click=_open_page,
                args=("Browse Campaigns",),
            )
        with play_col.container(border=True, height="stretch"):
            st.subheader("Play", icon=":material/sports_esports:")
            st.write("Resume your sessions and continue the narrative.")
            st.space("stretch")
            st.button(
                "My sessions",
                key="overview.play",
                icon=":material/arrow_forward:",
                width="stretch",
                on_click=_open_page,
                args=("My Sessions / Play",),
            )

        st.subheader("Application status", icon=":material/info:")
        st.metric("Active architecture", gateway.architecture, border=True)
    else:
        RENDERERS[page](ui)


def _open_page(page: str) -> None:
    st.session_state["page"] = page


class ScreenController:
    def __init__(self, gateway: ApplicationGateway, context: RequestContext) -> None:
        self.gateway, self.context = gateway, context

    def render(self, page: str) -> None:
        render(page, self.gateway, self.context)
