"""Short-lived, linked sidebar conversations for the Agent Workspace."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.application.deletion import delete_binding_records
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentSidebarConversation,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.tasks.public import enqueue
from flowweave.modules.users.application.security import current_user_id
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

_SIDEBAR_TTL = timedelta(hours=1)


def _source_binding(db: Session, workspace_id: str, binding_id: str) -> AgentConversationBinding:
    binding = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.id == binding_id,
            AgentConversationBinding.workspace_id == workspace_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
        )
    )
    if binding is None:
        raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "主会话不存在或已删除", 404)
    if (
        db.scalar(
            select(AgentSidebarConversation.id).where(
                AgentSidebarConversation.sidebar_binding_id == binding.id
            )
        )
        is not None
    ):
        raise DomainError("AGENT_SIDEBAR_SOURCE_INVALID", "临时侧边会话不能作为主会话", 422)
    return binding


def _source_context(source: AgentConversationBinding) -> str:
    return "\n".join(
        (
            "这是一个临时侧边聊天会话，已关联到用户当前打开的主会话。",
            "主会话元数据仅用于回答当前问题；不要声称拥有未提供的完整主会话历史。",
            "若当前消息包含引用，请优先基于引用内容回答，并明确区分引用与元数据。",
            f"主会话标题：{source.display_title or '未命名会话'}",
            f"主会话绑定标识：{source.id}",
            f"主会话模型：{source.model_name or '未记录'}",
            f"主会话创建时间：{source.created_at.isoformat()}",
        )
    )


def create_sidebar_conversation(
    db: Session,
    workspace_id: str,
    *,
    source_binding_id: str,
    conversation_id: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
    content: str,
    references: tuple[dict[str, str], ...],
    idempotency_key: str,
) -> dict[str, Any]:
    source = _source_binding(db, workspace_id, source_binding_id)
    capability_version_ids = tuple(
        item.capability_version_id
        for item in db.scalars(
            select(AgentConversationCapability)
            .where(AgentConversationCapability.binding_id == source.id)
            .order_by(AgentConversationCapability.position)
        )
    )
    result = conversations.bootstrap_conversation(
        db,
        workspace_id,
        work_directory_id=conversations._work_directory_id(db, source),
        conversation_id=conversation_id,
        model_provider_id=model_provider_id,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        content=content,
        references=references,
        capability_version_ids=capability_version_ids,
        idempotency_key=idempotency_key,
        sidebar_source_binding_id=source.id,
        system_message_suffix_append=_source_context(source),
    )
    binding_id = str(result["conversation"]["id"])
    expires_at = now() + _SIDEBAR_TTL
    existing = db.scalar(
        select(AgentSidebarConversation).where(
            AgentSidebarConversation.sidebar_binding_id == binding_id
        )
    )
    if existing is None:
        link = AgentSidebarConversation(
            workspace_id=workspace_id,
            owner_user_id=current_user_id(),
            source_binding_id=source.id,
            sidebar_binding_id=binding_id,
            expires_at=expires_at,
        )
        db.add(link)
        enqueue(
            db,
            task_type="EXPIRE_AGENT_SIDEBAR_CONVERSATION",
            aggregate_type="AGENT_SIDEBAR_CONVERSATION",
            aggregate_id=binding_id,
            idempotency_key=f"expire-agent-sidebar-conversation:{binding_id}",
            available_at=expires_at,
        )
        db.commit()
    else:
        expires_at = existing.expires_at
    return {**result, "expires_at": expires_at.isoformat()}


def sidebar_conversation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    link = db.scalar(
        select(AgentSidebarConversation).where(
            AgentSidebarConversation.workspace_id == workspace_id,
            AgentSidebarConversation.sidebar_binding_id == binding_id,
        )
    )
    if link is None:
        raise DomainError("AGENT_SIDEBAR_NOT_FOUND", "侧边聊天不存在", 404)
    if link.expired_at is not None or link.expires_at <= now():
        return {
            "binding_id": binding_id,
            "expires_at": link.expires_at.isoformat(),
            "expired": True,
        }
    return {
        "binding_id": binding_id,
        "source_binding_id": link.source_binding_id,
        "expires_at": link.expires_at.isoformat(),
        "expired": False,
    }


def assert_sidebar_writable(
    db: Session, workspace_id: str, binding_id: str
) -> AgentSidebarConversation | None:
    link = db.scalar(
        select(AgentSidebarConversation).where(
            AgentSidebarConversation.workspace_id == workspace_id,
            AgentSidebarConversation.sidebar_binding_id == binding_id,
        )
    )
    if link is None:
        return None
    if link.expired_at is not None or link.expires_at <= now():
        raise DomainError(
            "AGENT_SIDEBAR_CONVERSATION_EXPIRED", "侧边聊天会话已过期，无法继续发送消息", 410
        )
    return link


def _delete_sidebar_conversation(db: Session, link: AgentSidebarConversation) -> None:
    binding = db.get(AgentConversationBinding, link.sidebar_binding_id)
    if binding is not None:
        workspace = conversations._workspace(db, link.workspace_id)
        try:
            get_runtime().delete_conversation(conversations._handle(db, workspace, binding))
        except DomainError as exc:
            if exc.code != "RUNTIME_CONVERSATION_MISSING":
                raise
        agent_workspace_host.delete_session_attachment_files(db, link.workspace_id, binding.id)
        delete_binding_records(db, binding.id)
    link.expired_at = now()
    db.commit()


def close_sidebar_conversation(db: Session, workspace_id: str, binding_id: str) -> None:
    link = db.scalar(
        select(AgentSidebarConversation)
        .where(
            AgentSidebarConversation.workspace_id == workspace_id,
            AgentSidebarConversation.sidebar_binding_id == binding_id,
        )
        .with_for_update()
    )
    if link is None or link.expired_at is not None:
        return
    _delete_sidebar_conversation(db, link)


def expire_sidebar_conversation(db: Session, binding_id: str) -> None:
    link = db.scalar(
        select(AgentSidebarConversation)
        .where(AgentSidebarConversation.sidebar_binding_id == binding_id)
        .with_for_update()
    )
    if link is None or link.expired_at is not None or link.expires_at > now():
        return
    _delete_sidebar_conversation(db, link)


__all__ = (
    "assert_sidebar_writable",
    "close_sidebar_conversation",
    "create_sidebar_conversation",
    "expire_sidebar_conversation",
    "sidebar_conversation",
)
