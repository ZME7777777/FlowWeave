"""Durable, authorized search over native OpenHands Agent conversations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSearch,
    AgentConversationSearchHit,
)
from flowweave.modules.tasks.public import enqueue
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError, not_found


def _search(
    db: Session, workspace_id: str, search_id: str, *, lock: bool = False
) -> AgentConversationSearch:
    statement = select(AgentConversationSearch).where(
        AgentConversationSearch.id == search_id,
        AgentConversationSearch.workspace_id == workspace_id,
    )
    if lock:
        statement = statement.with_for_update()
    search = db.scalar(statement)
    if search is None:
        raise not_found("agent_conversation_search", search_id)
    return search


def start(db: Session, workspace_id: str, query: str) -> dict[str, Any]:
    conversations._workspace(db, workspace_id)
    normalized = query.strip()
    if not normalized:
        raise DomainError("AGENT_CONVERSATION_SEARCH_QUERY_INVALID", "请输入要搜索的内容", 422)
    search = AgentConversationSearch(workspace_id=workspace_id, query=normalized)
    db.add(search)
    db.flush()
    enqueue(
        db,
        task_type="SEARCH_AGENT_CONVERSATIONS",
        aggregate_type="AGENT_CONVERSATION_SEARCH",
        aggregate_id=search.id,
        idempotency_key=f"search-agent-conversations:{search.id}",
    )
    return _status(db, search, include_hits=False)


def _status(db: Session, search: AgentConversationSearch, *, include_hits: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": search.id,
        "query": search.query,
        "state": search.state,
        "failure_summary": search.failure_summary,
        "created_at": search.created_at.isoformat(),
        "completed_at": search.completed_at.isoformat() if search.completed_at else None,
    }
    if include_hits:
        value["hits"] = _hits(db, search)
    return value


def status(db: Session, workspace_id: str, search_id: str) -> dict[str, Any]:
    return _status(db, _search(db, workspace_id, search_id), include_hits=True)


def _hits(db: Session, search: AgentConversationSearch) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(AgentConversationSearchHit)
            .where(AgentConversationSearchHit.search_id == search.id)
            .order_by(AgentConversationSearchHit.created_at.desc())
        )
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        binding = db.scalar(
            select(AgentConversationBinding).where(
                AgentConversationBinding.id == row.binding_id,
                AgentConversationBinding.workspace_id == search.workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
        if binding is None:
            continue
        try:
            event = get_runtime().read_event(
                conversations._handle(
                    db, conversations._workspace(db, search.workspace_id), binding
                ),
                row.event_id,
            )
        except Exception:
            continue
        if event is None:
            continue
        raw_content = event.payload.get("content")
        content = (
            conversations._project_conversation_references(raw_content)[0]
            if isinstance(raw_content, str)
            else raw_content
        )
        if not isinstance(content, str) or not content.strip():
            continue
        if search.query.casefold() not in content.casefold():
            continue
        results.append(
            {
                "binding_id": binding.id,
                "event_id": row.event_id,
                "title": binding.display_title or "未命名会话",
                "content": content,
                "timestamp": event.payload.get("timestamp"),
                "source": event.payload.get("source"),
            }
        )
    return results


def process(db: Session, search_id: str) -> None:
    search = db.scalar(
        select(AgentConversationSearch)
        .where(AgentConversationSearch.id == search_id)
        .with_for_update()
    )
    if search is None or search.state == "SUCCEEDED":
        return
    if search.state == "FAILED":
        return
    workspace_id = search.workspace_id
    query = search.query
    search.state = "RUNNING"
    db.commit()
    # Never retain a database transaction or row lock across unbounded native
    # EventLog scans. The delivery lease keeps this Worker task authoritative.
    try:
        workspace = conversations._workspace(db, workspace_id)
        bindings = list(
            db.scalars(
                select(AgentConversationBinding)
                .where(
                    AgentConversationBinding.workspace_id == workspace_id,
                    AgentConversationBinding.lifecycle == "ACTIVE",
                )
                .order_by(AgentConversationBinding.created_at.desc())
            )
        )
        hit_keys: list[tuple[str, str]] = []
        for binding in bindings:
            for event in get_runtime().search_message_events(
                conversations._handle(db, workspace, binding), query
            ):
                hit_keys.append((binding.id, event.cursor))
    except Exception:
        search = db.scalar(
            select(AgentConversationSearch)
            .where(AgentConversationSearch.id == search_id)
            .with_for_update()
        )
        if search is not None:
            search.state = "FAILED"
            search.failure_summary = "搜索暂时无法完成，请稍后重新搜索"
            search.completed_at = datetime.now(UTC)
        return
    search = db.scalar(
        select(AgentConversationSearch)
        .where(AgentConversationSearch.id == search_id)
        .with_for_update()
    )
    if search is None or search.state == "SUCCEEDED":
        return
    existing = {
        (item.binding_id, item.event_id)
        for item in db.scalars(
            select(AgentConversationSearchHit).where(
                AgentConversationSearchHit.search_id == search.id
            )
        )
    }
    for binding_id, event_id in hit_keys:
        if (binding_id, event_id) not in existing:
            db.add(
                AgentConversationSearchHit(
                    search_id=search.id,
                    binding_id=binding_id,
                    event_id=event_id,
                )
            )
    search.state = "SUCCEEDED"
    search.completed_at = datetime.now(UTC)
    search.failure_summary = None
