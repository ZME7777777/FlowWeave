"""Child locator authorization; OpenHands remains the execution fact source."""

from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from flowweave.modules.agent_sessions.application.host import AgentSessionHostContext
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_user_id
from flowweave.shared.errors import DomainError


def ordinary_conversation_clause() -> ColumnElement[bool]:
    return ~exists().where(AgentChildConversation.child_binding_id == AgentConversationBinding.id)


def _not_found() -> DomainError:
    return DomainError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除", 404)


def require_direct_child(
    db: Session,
    host: AgentSessionHostContext,
    *,
    parent_binding_id: str,
    child_binding_id: str,
) -> tuple[AgentChildConversation, AgentConversationBinding, AgentConversationBinding]:
    """Host permissions must be checked before supplying this resolved context."""

    owner = current_user_id(default=FLOWWEAVE_USER_ID)
    relation = db.scalar(
        select(AgentChildConversation).where(
            AgentChildConversation.owner_user_id == owner,
            AgentChildConversation.parent_binding_id == parent_binding_id,
            AgentChildConversation.child_binding_id == child_binding_id,
            AgentChildConversation.host_kind == host.host_kind,
            AgentChildConversation.host_id == host.host_id,
            AgentChildConversation.conversation_scope_id == host.conversation_scope_id,
            AgentChildConversation.runtime_session_id == host.runtime_session_id,
        )
    )
    if relation is None:
        raise _not_found()
    bindings = list(
        db.scalars(
            select(AgentConversationBinding).where(
                AgentConversationBinding.owner_user_id == owner,
                AgentConversationBinding.id.in_((parent_binding_id, child_binding_id)),
                AgentConversationBinding.host_kind == host.host_kind,
                AgentConversationBinding.host_id == host.host_id,
                AgentConversationBinding.conversation_scope_id == host.conversation_scope_id,
                AgentConversationBinding.runtime_session_id == host.runtime_session_id,
                AgentConversationBinding.working_directory == host.working_directory,
            )
        )
    )
    by_id = {binding.id: binding for binding in bindings}
    parent, child = by_id.get(parent_binding_id), by_id.get(child_binding_id)
    if (
        parent is None
        or child is None
        or parent.id == child.id
        or parent.openhands_conversation_id != relation.parent_conversation_id
        or child.openhands_conversation_id != relation.child_conversation_id
        or parent.lifecycle not in {"ACTIVE", "DELETE_PENDING"}
        or child.node_attempt_id is not None
        or child.node_run_id is not None
        or db.scalar(
            select(AgentChildConversation.id).where(
                AgentChildConversation.child_binding_id == parent_binding_id
            )
        )
        is not None
    ):
        raise _not_found()
    return relation, parent, child


def verify_native_child(
    relation: AgentChildConversation,
    *,
    conversation_id: str,
    parent_conversation_id: str,
    working_directory: str,
    expected_working_directory: str,
    isolation: str,
) -> None:
    """Compare formal native response fields; never infer lineage from events."""
    if (
        conversation_id != relation.child_conversation_id
        or parent_conversation_id != relation.parent_conversation_id
        or working_directory != expected_working_directory
        or isolation != "shared"
    ):
        raise DomainError("AGENT_CHILD_NATIVE_IDENTITY_MISMATCH", "原生子会话身份不匹配", 409)


def require_ordinary_binding(
    db: Session, binding: AgentConversationBinding, *, parent_redirect: bool = False
) -> None:
    """Keep all generic reads and writes outside the dedicated child surface."""
    if binding.owner_user_id != current_user_id(default=FLOWWEAVE_USER_ID):
        raise _not_found()
    relation = db.scalar(
        select(AgentChildConversation).where(
            AgentChildConversation.owner_user_id == current_user_id(default=FLOWWEAVE_USER_ID),
            AgentChildConversation.child_binding_id == binding.id,
        )
    )
    if relation is None:
        return
    if not parent_redirect:
        raise _not_found()
    host = AgentSessionHostContext.create(
        host_kind=binding.host_kind,
        host_id=binding.host_id,
        conversation_scope_id=binding.conversation_scope_id,
        runtime_session_id=binding.runtime_session_id,
        working_directory=binding.working_directory or "",
    )
    _, parent, _ = require_direct_child(
        db, host, parent_binding_id=relation.parent_binding_id, child_binding_id=binding.id
    )
    # Only expose an already-authorized parent locator, never native IDs or URLs.
    raise DomainError(
        "AGENT_CHILD_REQUIRES_PARENT",
        "请从父会话查看子会话",
        409,
        {"binding_id": binding.id, "parent_binding_id": parent.id},
    )


def guard_main_route(
    db: Session,
    *,
    host_kind: str,
    host_id: str,
    binding_id: str,
    conversation_scope_id: str | None = None,
) -> AgentConversationBinding:
    query = select(AgentConversationBinding).where(
        AgentConversationBinding.owner_user_id == current_user_id(default=FLOWWEAVE_USER_ID),
        AgentConversationBinding.host_kind == host_kind,
        AgentConversationBinding.host_id == host_id,
        AgentConversationBinding.id == binding_id,
    )
    if host_kind == "AGENT_WORKSPACE":
        query = query.where(AgentConversationBinding.workspace_id == host_id)
    elif host_kind == "FLOW_NODE":
        query = query.where(AgentConversationBinding.flow_run_id == host_id)
    if conversation_scope_id is not None:
        query = query.where(
            or_(
                AgentConversationBinding.conversation_scope_id == conversation_scope_id,
                AgentConversationBinding.node_attempt_id == conversation_scope_id,
            )
        )
    binding = db.scalar(query)
    if binding is None:
        raise _not_found()
    require_ordinary_binding(db, binding, parent_redirect=True)
    return binding


def require_unlinked_deletion(db: Session, binding_id: str) -> None:
    if (
        db.scalar(
            select(AgentChildConversation.id).where(
                or_(
                    AgentChildConversation.parent_binding_id == binding_id,
                    AgentChildConversation.child_binding_id == binding_id,
                )
            )
        )
        is not None
    ):
        raise DomainError(
            "AGENT_CHILDREN_DELETE_REQUIRED", "该会话关联子会话，需要先完成子会话清理", 409
        )
