import streamlit as st

from shared.commands import Login, Logout, RegisterAccount
from shared.models.accounts import AccountView, LoginResult
from shared.models.common import SecretInput

from .common import UIContext, consume_completed, reset_account_state, submit


def account_control(ui: UIContext, signed_in: AccountView | None) -> None:
    """Render the compact account control used in the application header."""
    if signed_in is not None:
        action = st.menu_button(
            signed_in.username,
            ("Sign out",),
            key="ui.account.menu",
            icon=":material/account_circle:",
            help="Account menu",
            format_func=lambda value: f":material/logout: {value}",
        )
        if action == "Sign out":
            submit(ui, "account.logout", Logout())
        logout_receipt = consume_completed(ui, "account.logout")
        if logout_receipt is not None or st.session_state.get(
            "ui.account.logout.handled_operation_id"
        ):
            reset_account_state()
            st.rerun()
        return

    with st.popover(
        "Sign in",
        icon=":material/account_circle:",
    ):
        login_tab, register_tab = st.tabs(("Sign in", "Create account"))
        with login_tab:
            with st.form("login_form", clear_on_submit=True):
                username = st.text_input("Username", key="login_username")
                password = st.text_input("Password", type="password", key="login_password")
                login = st.form_submit_button("Sign in", type="primary", width="stretch")
            if login:
                submit(ui, "account.login", Login(username, SecretInput(password)))
            logged_in = consume_completed(ui, "account.login")
            if isinstance(logged_in, LoginResult):
                st.rerun()

        with register_tab:
            with st.form("register_form", clear_on_submit=True):
                username = st.text_input("Username", key="register_username")
                password = st.text_input("Password", type="password", key="register_password")
                register = st.form_submit_button("Create account", type="primary", width="stretch")
            if register:
                submit(ui, "account.register", RegisterAccount(username, SecretInput(password)))
            registered = consume_completed(ui, "account.register")
            if isinstance(registered, AccountView):
                st.success(f"Account {registered.username} created. Sign in to continue.")
