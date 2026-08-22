from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.conversations.infrastructure.models import (
    FlowRunConversationBinding,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.base import RuntimeHandle
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class FlowRunConversationLocator:
    flow_run_id: str
    runtime_session_id: str
    openhands_conversation_id: str


def bind_openhands_conversation(
    db: Session,
    *,
    flow_run_id: str,
    openhands_conversation_id: str,
    display_label: str | None = None,
    binding_id: str | None = None,
) -> FlowRunConversationBinding:
    """Idempotently bind an OpenHands identity to the FlowRun Runtime Session."""

    if not openhands_conversation_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_ID_REQUIRED",
            "OpenHands omitted the Conversation identity",
            502,
        )
    connection = sandboxes.active_flow_run_runtime_connection(
        db, flow_run_id=flow_run_id
    )
    item = db.scalar(
        select(FlowRunConversationBinding)
        .where(
            FlowRunConversationBinding.runtime_session_id
            == connection.runtime_session_id,
            FlowRunConversationBinding.openhands_conversation_id
            == openhands_conversation_id,
        )
        .with_for_update()
    )
    if item is None:
        item = FlowRunConversationBinding(
            **({"id": binding_id} if binding_id is not None else {}),
            flow_run_id=flow_run_id,
            runtime_session_id=connection.runtime_session_id,
            openhands_conversation_id=openhands_conversation_id,
            display_label=display_label,
        )
        db.add(item)
    else:
        if item.flow_run_id != flow_run_id:
            raise DomainError(
                "RUNTIME_CONVERSATION_OWNER_CONFLICT",
                "The OpenHands Conversation belongs to another FlowRun",
                409,
                {"openhands_conversation_id": openhands_conversation_id},
            )
        if display_label is not None:
            item.display_label = display_label
        item.last_connected_at = now()
    db.flush()
    return item


def conversation_locator(
    db: Session,
    *,
    flow_run_id: str,
    openhands_conversation_id: str,
) -> FlowRunConversationLocator:
    """Load the minimal locator and fail closed for legacy unbound conversations."""

    item = db.scalar(
        select(FlowRunConversationBinding).where(
            FlowRunConversationBinding.flow_run_id == flow_run_id,
            FlowRunConversationBinding.openhands_conversation_id
            == openhands_conversation_id,
        )
    )
    if item is None:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNBOUND",
            "The OpenHands Conversation is not bound to this FlowRun Runtime Session",
            409,
            {
                "flow_run_id": flow_run_id,
                "openhands_conversation_id": openhands_conversation_id,
            },
        )
    return FlowRunConversationLocator(
        flow_run_id=item.flow_run_id,
        runtime_session_id=item.runtime_session_id,
        openhands_conversation_id=item.openhands_conversation_id,
    )


def conversation_binding(
    db: Session,
    *,
    flow_run_id: str,
    openhands_conversation_id: str,
) -> FlowRunConversationBinding:
    """Return the active locator row without adding Conversation semantics."""

    item = db.scalar(
        select(FlowRunConversationBinding).where(
            FlowRunConversationBinding.flow_run_id == flow_run_id,
            FlowRunConversationBinding.openhands_conversation_id
            == openhands_conversation_id,
        )
    )
    if item is None:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNBOUND",
            "The OpenHands Conversation is not bound to this FlowRun Runtime Session",
            409,
            {
                "flow_run_id": flow_run_id,
                "openhands_conversation_id": openhands_conversation_id,
            },
        )
    return item


def binding_locator(db: Session, binding_id: str) -> FlowRunConversationLocator:
    item = db.get(FlowRunConversationBinding, binding_id)
    if item is None:
        raise DomainError(
            "RUNTIME_CONVERSATION_UNBOUND",
            "The FlowRun Conversation locator is unavailable",
            404,
            {"binding_id": binding_id},
        )
    return FlowRunConversationLocator(
        flow_run_id=item.flow_run_id,
        runtime_session_id=item.runtime_session_id,
        openhands_conversation_id=item.openhands_conversation_id,
    )


def active_runtime_handle(
    db: Session,
    *,
    flow_run_id: str,
    openhands_conversation_id: str,
    cursor: str | None,
    route_kind: str,
) -> RuntimeHandle:
    """Route a Conversation through its FlowRun's current active generation."""

    locator = conversation_locator(
        db,
        flow_run_id=flow_run_id,
        openhands_conversation_id=openhands_conversation_id,
    )
    connection = sandboxes.active_flow_run_runtime_connection(
        db, flow_run_id=flow_run_id
    )
    if connection.runtime_session_id != locator.runtime_session_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_SESSION_DRIFT",
            "The Conversation locator no longer matches the FlowRun Runtime Session",
            409,
            {
                "flow_run_id": flow_run_id,
                "runtime_session_id": locator.runtime_session_id,
            },
        )
    prefix = "env-exec" if route_kind == "EXECUTION" else "env-chat"
    return RuntimeHandle(
        job_id=f"{prefix}:{connection.resource_name}",
        conversation_id=locator.openhands_conversation_id,
        cursor=cursor,
        runtime_resource_id=connection.managed_runtime_id,
        runtime_resource_name=connection.resource_name,
    )


__all__ = (
    "FlowRunConversationLocator",
    "active_runtime_handle",
    "binding_locator",
    "bind_openhands_conversation",
    "conversation_binding",
    "conversation_locator",
)
