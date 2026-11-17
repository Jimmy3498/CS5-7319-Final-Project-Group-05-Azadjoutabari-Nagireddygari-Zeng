"""Shared deterministic setup for cross-architecture risk experiments."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from event_based.bootstrap import build as build_event
from layered.bootstrap import build as build_layered
from shared import commands as c
from shared.codec import decode, load_json
from shared.errors import ApplicationError, ErrorCode
from shared.gateway import OperationReceipt, OperationStatus
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.ai import ProviderCapture, ProviderMessage, ProviderResponse
from shared.models.common import RequestContext, SecretInput
from shared.models.views import TurnResultView
from shared.models.world import CampaignDraft

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_CANARY = "SPOILER-CANARY-CEDRIC"
CREDENTIAL_CANARY = "RISK-KEY-CANARY-DO-NOT-EXPOSE"


class MemoryVault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], SecretInput] = {}

    def write(self, owner_id: str, reference: str, value: SecretInput) -> None:
        self.values[owner_id, reference] = value

    def read(self, owner_id: str, reference: str) -> SecretInput:
        try:
            return self.values[owner_id, reference]
        except KeyError:
            raise ApplicationError(ErrorCode.INVALID, "API key not found") from None

    def remove(self, owner_id: str, reference: str) -> None:
        self.values.pop((owner_id, reference), None)


@dataclass(frozen=True)
class ProviderCall:
    messages: tuple[tuple[str, str], ...]
    credential_present: bool

    @property
    def text(self) -> str:
        return "\n".join(content for _, content in self.messages)


class RecordingProvider:
    mode = "risk-recording-provider"

    def __init__(self) -> None:
        self._responses = deque(
            (
                ProviderResponse(
                    json.dumps(
                        {
                            "expected_revision": 0,
                            "effects": [],
                            "public_fact_ids": [],
                        }
                    )
                ),
                ProviderResponse("You wait and watch the room."),
            )
        )
        self.calls: list[ProviderCall] = []

    def complete(
        self, messages: tuple[ProviderMessage, ...], capture: ProviderCapture
    ) -> ProviderResponse:
        if not self._responses:
            raise ApplicationError(ErrorCode.PROVIDER, "Risk-provider script exhausted")
        self.calls.append(
            ProviderCall(
                tuple((message.role, message.content) for message in messages),
                bool(capture.credential.value),
            )
        )
        return self._responses.popleft()


@dataclass(frozen=True)
class SeededScenario:
    architecture: str
    context: RequestContext
    owner_id: str
    campaign_id: str
    version_id: str
    session_id: str
    turn_command: c.SubmitTurn
    store: SQLiteStore


def build_application(
    architecture: str,
    data_root: Path,
    provider: RecordingProvider,
    vault: MemoryVault,
):
    with (
        patch("shared.wiring.CompatibleHttpProvider", return_value=provider),
        patch("shared.wiring.LocalCredentialStore", return_value=vault),
    ):
        if architecture == "Layered":
            return build_layered(data_root)
        if architecture == "Event-Based":
            return build_event(data_root, start_worker=False)
    raise ValueError(f"Unknown architecture: {architecture}")


def submit_and_drain(
    app, architecture: str, context: RequestContext, command: c.Command, request_id: str
) -> OperationReceipt:
    receipt = app.submit(context, command, request_id)
    if architecture == "Event-Based":
        while app.bus.pending:
            if not app.bus.dispatch_one():
                raise RuntimeError("Event bus stopped before completing queued work")
        receipt = app.poll(context, receipt.operation_id)
    if receipt.status != OperationStatus.COMPLETED:
        raise RuntimeError(f"Scenario setup failed: {receipt.status}: {receipt.message}")
    return receipt


def seed_scenario(app, architecture: str, data_root: Path) -> SeededScenario:
    identity = uuid4().hex
    context = RequestContext(f"risk-browser-{identity}", None, f"risk-{identity}")
    password = SecretInput("risk-demo-password")
    account = submit_and_drain(
        app,
        architecture,
        context,
        c.RegisterAccount(f"risk-{identity}", password),
        f"register-{identity}",
    ).result
    submit_and_drain(
        app,
        architecture,
        context,
        c.Login(f"risk-{identity}", password),
        f"login-{identity}",
    )
    card = submit_and_drain(
        app,
        architecture,
        context,
        c.SaveProviderCard(
            None,
            None,
            "Risk provider",
            "https://example.test/chat",
            "risk-model",
            "chat-completions-compatible",
            SecretInput(CREDENTIAL_CANARY),
        ),
        f"card-{identity}",
    ).result
    submit_and_drain(
        app,
        architecture,
        context,
        c.ActivateProviderCard(card.id, card.configuration_revision),
        f"activate-{identity}",
    )

    fixture_path = ROOT / "fixtures" / "thornmere.json"
    fixture = decode(load_json(fixture_path.read_text(encoding="utf-8"))["draft"], CampaignDraft)
    if PRIVATE_CANARY not in fixture_path.read_text(encoding="utf-8"):
        raise RuntimeError("Private canary is missing from the declared fixture")
    draft = submit_and_drain(
        app,
        architecture,
        context,
        c.CreateCampaign("Risk campaign", "Controlled risk scenario"),
        f"create-{identity}",
    ).result
    draft = submit_and_drain(
        app,
        architecture,
        context,
        c.SaveDraft(draft.id, draft.revision, fixture.content),
        f"save-{identity}",
    ).result
    published = submit_and_drain(
        app,
        architecture,
        context,
        c.PublishCampaign(draft.id, draft.revision),
        f"publish-{identity}",
    ).result
    session = submit_and_drain(
        app,
        architecture,
        context,
        c.StartSession(published.published_version_id, "rowan"),
        f"session-{identity}",
    ).result
    return SeededScenario(
        architecture=architecture,
        context=context,
        owner_id=account.id,
        campaign_id=draft.id,
        version_id=published.published_version_id,
        session_id=session.session_id,
        turn_command=c.SubmitTurn(session.session_id, 0, "Wait and observe"),
        store=SQLiteStore(data_root / "application.sqlite3"),
    )


def execute_turn(app, scenario: SeededScenario, request_id: str) -> OperationReceipt:
    receipt = submit_and_drain(
        app,
        scenario.architecture,
        scenario.context,
        scenario.turn_command,
        request_id,
    )
    if not isinstance(receipt.result, TurnResultView):
        raise RuntimeError("Turn scenario did not return a TurnResultView")
    return receipt
