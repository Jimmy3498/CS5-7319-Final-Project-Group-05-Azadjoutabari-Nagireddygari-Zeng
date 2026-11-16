import streamlit as st

from shared.commands import ActivateProviderCard, DeleteProviderCard, SaveProviderCard
from shared.models.ai import ProviderProfile
from shared.models.common import SecretInput
from shared.queries import Query, QueryKind

from .common import UIContext, consume_completed, query, require_account, submit


def profiles(ui: UIContext) -> None:
    st.header("Private API cards")
    if require_account(ui) is None:
        return

    cards = query(ui, Query(QueryKind.PROVIDER_CARDS))
    if cards is None:
        return
    active = query(ui, Query(QueryKind.ACTIVE_PROVIDER_CARD), show_error=False)
    active_id = active.id if isinstance(active, ProviderProfile) else None

    choices = [None, *(card.id for card in cards)]
    selected_id = st.selectbox(
        "Card",
        choices,
        format_func=lambda identity: (
            "Create a new card"
            if identity is None
            else next(card.name for card in cards if card.id == identity)
        ),
        key="ui.profiles.selected",
    )
    selected = next((card for card in cards if card.id == selected_id), None)
    if selected is not None and selected.id == active_id:
        st.success("Active for the next AI request")

    presets = {
        "OpenAI": (
            "chat-completions-compatible",
            "https://api.openai.com/v1/chat/completions",
            "gpt-6-luna",
        ),
        "Anthropic": (
            "anthropic-messages",
            "https://api.anthropic.com/v1/messages",
            "claude-sonnet-5",
        ),
        "Google Gemini": (
            "google-generate-content",
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            "gemini-3.8-flash",
        ),
        "Custom compatible endpoint": ("chat-completions-compatible", "", ""),
    }
    preset_name = None
    if selected is None:
        preset_name = st.selectbox("Provider preset", tuple(presets))
        default_protocol, default_endpoint, default_model = presets[preset_name]
    else:
        default_protocol, default_endpoint, default_model = (
            selected.protocol,
            selected.endpoint,
            selected.model,
        )

    with st.form("provider_card_form", clear_on_submit=True):
        name = st.text_input("Card name", value=selected.name if selected else (preset_name or ""))
        endpoint = st.text_input(
            "Full provider endpoint",
            value=default_endpoint,
            placeholder="https://api.openai.com/v1/chat/completions",
        )
        model = st.text_input("Model ID", value=default_model)
        protocols = (
            "chat-completions-compatible",
            "anthropic-messages",
            "google-generate-content",
        )
        protocol = st.selectbox(
            "Protocol",
            protocols,
            index=protocols.index(default_protocol),
            format_func=lambda value: {
                "chat-completions-compatible": "OpenAI / compatible (including Meta hosts)",
                "anthropic-messages": "Anthropic Messages",
                "google-generate-content": "Google Gemini generateContent",
            }[value],
        )
        st.caption(
            "Gemini endpoints may use `{model}`, for example: "
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        )
        credential = st.text_input(
            "API key",
            type="password",
            help="Leave blank to preserve the existing key. Re-enter it when changing endpoint.",
        )
        save = st.form_submit_button("Save card", type="primary")
    if save:
        submit(
            ui,
            "profiles.save",
            SaveProviderCard(
                selected.id if selected else None,
                selected.configuration_revision if selected else None,
                name,
                endpoint,
                model,
                protocol,
                SecretInput(credential) if credential else None,
            ),
        )
    saved = consume_completed(ui, "profiles.save")
    if isinstance(saved, ProviderProfile):
        st.session_state["ui.profiles.selected"] = saved.id
        st.rerun()

    if selected is None:
        if not cards:
            st.info("Save a provider card, then activate it before using AI features.")
        return

    activate_col, delete_col = st.columns(2)
    with activate_col:
        if st.button("Activate card", disabled=selected.id == active_id, width="stretch"):
            submit(
                ui,
                "profiles.activate",
                ActivateProviderCard(selected.id, selected.configuration_revision),
            )
    activated = consume_completed(ui, "profiles.activate")
    if isinstance(activated, ProviderProfile):
        st.rerun()

    replacement_options = [None, *(card.id for card in cards if card.id != selected.id)]
    replacement = st.selectbox(
        "Replacement if deleting the active card",
        replacement_options,
        format_func=lambda identity: (
            "Disable AI"
            if identity is None
            else next(card.name for card in cards if card.id == identity)
        ),
        disabled=selected.id != active_id,
    )
    with delete_col:
        if st.button("Delete card", width="stretch"):
            submit(
                ui,
                "profiles.delete",
                DeleteProviderCard(
                    selected.id,
                    selected.configuration_revision,
                    replacement if selected.id == active_id else None,
                ),
            )
    deleted = consume_completed(ui, "profiles.delete")
    if deleted is not None or st.session_state.get("ui.profiles.delete.handled_operation_id"):
        st.session_state["ui.profiles.selected"] = None
        st.rerun()

    st.caption(
        "Keys are stored outside SQLite in the configured OS credential vault. "
        "Saving or activating a card does not call the provider."
    )
