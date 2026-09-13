"""User collaboration annotations for either shared-session host.

OpenHands remains authoritative for messages and replies.  This module stores
only user-authored anchors/comments and never reads or validates model output.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationAnnotation
from flowweave.shared.errors import DomainError


def _view(item: AgentConversationAnnotation) -> dict[str, Any]:
    return {"id": item.id, "anchor_kind": item.anchor_kind, "anchor": item.anchor_json,
            "comment": item.comment, "state": item.state,
            "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


def list_annotations(db: Session, binding_id: str) -> list[dict[str, Any]]:
    return [_view(item) for item in db.scalars(select(AgentConversationAnnotation).where(
        AgentConversationAnnotation.binding_id == binding_id).order_by(
        AgentConversationAnnotation.created_at.asc(), AgentConversationAnnotation.id.asc()))]


def create_annotation(db: Session, *, binding_id: str, anchor_kind: str, anchor: dict[str, Any], comment: str) -> dict[str, Any]:
    value = comment.strip()
    if not value:
        raise DomainError("AGENT_ANNOTATION_COMMENT_EMPTY", "注释评论不能为空", 422)
    item = AgentConversationAnnotation(binding_id=binding_id, anchor_kind=anchor_kind, anchor_json=anchor, comment=value)
    db.add(item)
    db.flush()
    return _view(item)


def delete_annotation(db: Session, *, binding_id: str, annotation_id: str) -> None:
    deleted = db.execute(delete(AgentConversationAnnotation).where(
        AgentConversationAnnotation.id == annotation_id, AgentConversationAnnotation.binding_id == binding_id))
    if not deleted.rowcount:
        raise DomainError("AGENT_ANNOTATION_NOT_FOUND", "注释不存在或已删除", 404)


def prompt_annotations(db: Session, binding_id: str) -> tuple[dict[str, Any], ...]:
    return tuple({"annotation_id": item.id, "anchor_kind": item.anchor_kind, "anchor": item.anchor_json, "comment": item.comment}
        for item in db.scalars(select(AgentConversationAnnotation).where(
            AgentConversationAnnotation.binding_id == binding_id, AgentConversationAnnotation.state == "OPEN").order_by(
            AgentConversationAnnotation.created_at.asc(), AgentConversationAnnotation.id.asc())))


__all__ = ("create_annotation", "delete_annotation", "list_annotations", "prompt_annotations")
