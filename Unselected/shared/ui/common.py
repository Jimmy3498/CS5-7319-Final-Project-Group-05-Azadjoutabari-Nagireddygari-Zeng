from dataclasses import dataclass
from uuid import uuid4

import streamlit as st

from shared.commands import Command
from shared.errors import ApplicationError, ErrorCode
from shared.gateway import ApplicationGateway, OperationReceipt, OperationStatus
from shared.models.accounts import AccountView
from shared.models.common import RequestContext
from shared.queries import Query, QueryKind


@dataclass(frozen=True)
class UIContext:
    gateway: ApplicationGateway
    request: RequestContext

    @property
    def architecture(self) -> str:
        return self.gateway.architecture


def _key(scope: str, name: str) -> str:
    return f"ui.{scope}.{name}"


def reset_account_state() -> None:
    for key in tuple(st.session_state):
        if str(key).startswith("ui."):
            del st.session_state[key]


def query(ui: UIContext, value: Query, *, show_error: bool = True):
    try:
        return ui.gateway.query(ui.request, value)
    except ApplicationError as error:
        if show_error and error.code is not ErrorCode.UNAUTHENTICATED:
            st.error(error.public_message)
        return None
    except Exception:
        if show_error:
            st.error("The requested information could not be loaded.")
        return None


def current_account(ui: UIContext) -> AccountView | None:
    result = query(ui, Query(QueryKind.CURRENT_ACCOUNT), show_error=False)
    return result if isinstance(result, AccountView) else None


def require_account(ui: UIContext) -> AccountView | None:
    account = current_account(ui)
    if account is None:
        st.info("Sign in from the profile control at the top right to use this workspace.")
    return account


def submit(ui: UIContext, scope: str, command: Command) -> OperationReceipt:
    request_id = str(uuid4())
    receipt = ui.gateway.submit(ui.request, command, request_id)
    st.session_state[_key(scope, "operation_id")] = receipt.operation_id
    st.session_state[_key(scope, "receipt")] = receipt
    return receipt


def operation(ui: UIContext, scope: str) -> OperationReceipt | None:
    operation_id = st.session_state.get(_key(scope, "operation_id"))
    if not operation_id:
        return None
    try:
        receipt = ui.gateway.poll(ui.request, operation_id)
    except ApplicationError as error:
        st.error(error.public_message)
        return None
    except Exception:
        st.error("The operation status could not be loaded.")
        return None
    st.session_state[_key(scope, "receipt")] = receipt
    if receipt.status in {OperationStatus.PENDING, OperationStatus.RUNNING}:
        st.info(receipt.message)
        st.button("Refresh status", key=_key(scope, "refresh"))
    elif receipt.status is OperationStatus.COMPLETED:
        st.success(receipt.message)
    elif receipt.status in {OperationStatus.REJECTED, OperationStatus.UNIMPLEMENTED}:
        st.warning(receipt.message)
    else:
        st.error(receipt.message)
    return receipt


def consume_completed(ui: UIContext, scope: str):
    receipt = operation(ui, scope)
    if receipt is None or receipt.status is not OperationStatus.COMPLETED:
        return None
    handled_key = _key(scope, "handled_operation_id")
    if st.session_state.get(handled_key) == receipt.operation_id:
        return None
    st.session_state[handled_key] = receipt.operation_id
    return receipt.result


def clear_operation(scope: str) -> None:
    for name in ("operation_id", "receipt", "handled_operation_id"):
        st.session_state.pop(_key(scope, name), None)
