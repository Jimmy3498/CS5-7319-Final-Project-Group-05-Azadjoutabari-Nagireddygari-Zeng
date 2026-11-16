"""Shared Streamlit shell. Each application supplies its own gateway factory."""

import atexit
from collections.abc import Callable
from uuid import uuid4

import streamlit as st

from shared.gateway import ApplicationGateway
from shared.models.common import RequestContext
from shared.ui.accounts import account_control
from shared.ui.common import UIContext, current_account
from shared.ui.shell import PAGES, ScreenController


@st.cache_resource
def _host(architecture: str, _build: Callable[[], ApplicationGateway]) -> ApplicationGateway:
    host = _build()
    atexit.register(host.close)
    return host


def run(architecture: str, build: Callable[[], ApplicationGateway]) -> None:
    st.set_page_config(
        page_title=f"Story Studio | {architecture}",
        page_icon=":material/auto_stories:",
        layout="wide",
    )
    st.session_state.setdefault("_client_session_id", str(uuid4()))
    st.session_state.setdefault("_correlation_id", str(uuid4()))
    host = _host(architecture, build)
    context = RequestContext(
        st.session_state["_client_session_id"],
        None,
        st.session_state["_correlation_id"],
    )
    ui = UIContext(host, context)
    account = current_account(ui)

    st.sidebar.title("Story Studio")
    st.sidebar.caption(f"{architecture} architecture")
    page = st.sidebar.radio(
        "Workspace",
        PAGES,
        key="page",
        label_visibility="collapsed",
    )

    title_col, account_col = st.columns([5, 2], vertical_alignment="center")
    with title_col:
        st.title(":material/auto_stories: AI-powered storytelling")
    with account_col:
        with st.container(horizontal_alignment="right"):
            account_control(ui, account)

    ScreenController(host, context).render(page)
