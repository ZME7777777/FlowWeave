from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.public import AgentConversationBinding
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.runtime.base import RuntimeHandle
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

_FLOW_NODE = "FLOW_NODE"


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
    node_run_id: str | None = None,
    node_attempt_id: str | None = None,
    working_directory: str | None = None,
    work_directory_version_id: str | None = None,
    allow_inactive_session: bool = False,
) -> AgentConversationBinding:
    """Bind a FlowRun-native identity on the shared session locator."""

    if not openhands_conversation_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_ID_REQUIRED",
            "OpenHands omitted the Conversation identity",
            502,
        )
    if not node_attempt_id:
        raise DomainError(
            "NODE_CONVERSATION_CONTEXT_REQUIRED",
            "A FlowRun Conversation must belong to a Node Attempt",
            409,
            {"flow_run_id": flow_run_id},
        )
    # A native Conversation is only bound after its Attempt Runtime is active.
    # ``allow_inactive_session`` is retained for API compatibility but cannot
    # weaken this ownership boundary.
    del allow_inactive_session
    runtime_session_id = sandboxes.active_node_attempt_runtime_connection(
        db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id
    ).runtime_session_id
    item = db.scalar(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.runtime_session_id == runtime_session_id,
            AgentConversationBinding.openhands_conversation_id == openhands_conversation_id,
        )
        .with_for_update()
    )
    if item is None:
        item = AgentConversationBinding(
            **({"id": binding_id} if binding_id is not None else {}),
            workspace_id=None,
            runtime_session_id=runtime_session_id,
            host_kind=_FLOW_NODE,
            host_id=flow_run_id,
            # The Attempt owns the runtime, persistence and conversation
            # scope.  Different Attempts never address the same event tree.
            conversation_scope_id=node_attempt_id,
            flow_run_id=flow_run_id,
            node_run_id=node_run_id,
            node_attempt_id=node_attempt_id,
            working_directory=working_directory,
            work_directory_version_id=work_directory_version_id,
            openhands_conversation_id=openhands_conversation_id,
            display_title=display_label,
            lifecycle="ACTIVE",
            create_idempotency_key=(
                f"flow-run-native:{runtime_session_id}:{binding_id or openhands_conversation_id}"
            ),
        )
        db.add(item)
    else:
        if item.host_kind != _FLOW_NODE or item.flow_run_id != flow_run_id:
            raise DomainError(
                "RUNTIME_CONVERSATION_OWNER_CONFLICT",
                "The OpenHands Conversation belongs to another FlowRun",
                409,
                {"openhands_conversation_id": openhands_conversation_id},
            )
        if display_label is not None:
            item.display_title = display_label
        item.lifecycle = "ACTIVE"
        item.last_connected_at = now()
    db.flush()
    return item


def _flow_run_binding(
    db: Session, *, flow_run_id: str, openhands_conversation_id: str
) -> AgentConversationBinding:
    item = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.host_kind == _FLOW_NODE,
            AgentConversationBinding.flow_run_id == flow_run_id,
            AgentConversationBinding.openhands_conversation_id == openhands_conversation_id,
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


def conversation_locator(
    db: Session, *, flow_run_id: str, openhands_conversation_id: str
) -> FlowRunConversationLocator:
    item = _flow_run_binding(
        db, flow_run_id=flow_run_id, openhands_conversation_id=openhands_conversation_id
    )
    return FlowRunConversationLocator(
        flow_run_id=flow_run_id,
        runtime_session_id=item.runtime_session_id,
        openhands_conversation_id=item.openhands_conversation_id,
    )


def conversation_binding(
    db: Session, *, flow_run_id: str, openhands_conversation_id: str
) -> AgentConversationBinding:
    return _flow_run_binding(
        db, flow_run_id=flow_run_id, openhands_conversation_id=openhands_conversation_id
    )


def binding_locator(db: Session, binding_id: str) -> FlowRunConversationLocator:
    item = db.get(AgentConversationBinding, binding_id)
    if item is None or item.host_kind != _FLOW_NODE or item.flow_run_id is None:
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
        db, flow_run_id=flow_run_id, openhands_conversation_id=openhands_conversation_id
    )
    binding = _flow_run_binding(
        db, flow_run_id=flow_run_id, openhands_conversation_id=openhands_conversation_id
    )
    if not binding.node_attempt_id:
        raise DomainError(
            "RUNTIME_CONVERSATION_SESSION_DRIFT",
            "The Conversation has no Node Attempt Runtime owner",
            409,
            {"flow_run_id": flow_run_id, "binding_id": binding.id},
        )
    connection = sandboxes.active_node_attempt_runtime_connection(
        db, flow_run_id=flow_run_id, node_attempt_id=binding.node_attempt_id
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
