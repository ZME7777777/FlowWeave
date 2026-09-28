"""Durable, authorized search over native OpenHands Agent conversations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSearch,
    AgentConversationSearchHit,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectory,
    AgentWorkDirectoryVersion,
    AgentWorkspace,
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


def _selected_work_directory_ids(
    db: Session,
    workspace_id: str,
    work_directory_ids: list[str] | None,
    *,
    include_root: bool,
) -> list[str] | None:
    """Validate a durable search scope against the caller's workspace."""

    if work_directory_ids is None:
        return None
    selected = list(dict.fromkeys(work_directory_ids))
    if not selected and not include_root:
        raise DomainError(
            "AGENT_CONVERSATION_SEARCH_SCOPE_INVALID", "请至少选择一个工作区", 422
        )
    known_ids = set(
        db.scalars(
            select(AgentWorkDirectory.id).where(
                AgentWorkDirectory.workspace_id == workspace_id,
                AgentWorkDirectory.id.in_(selected),
            )
        )
    )
    if set(selected) != known_ids:
        raise DomainError(
            "AGENT_CONVERSATION_SEARCH_SCOPE_INVALID", "所选工作区不存在或已不可用", 422
        )
    return selected


def start(
    db: Session,
    workspace_id: str,
    query: str,
    *,
    work_directory_ids: list[str] | None = None,
    include_root: bool = True,
) -> dict[str, Any]:
    # Serialize starts for one Agent Workspace. A native full-text scan is
    # deliberately low priority, so launching several scans against the same
    # Runtime must never multiply pressure on a simultaneous hydration.
    workspace = db.scalar(
        select(AgentWorkspace).where(AgentWorkspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise not_found("agent_workspace", workspace_id)
    normalized = query.strip()
    if not normalized:
        raise DomainError("AGENT_CONVERSATION_SEARCH_QUERY_INVALID", "请输入要搜索的内容", 422)
    existing = db.scalar(
        select(AgentConversationSearch.id).where(
            AgentConversationSearch.workspace_id == workspace_id,
            AgentConversationSearch.state.in_(("PENDING", "RUNNING")),
        )
    )
    if existing is not None:
        raise DomainError(
            "AGENT_CONVERSATION_SEARCH_IN_PROGRESS",
            "当前工作区已有搜索任务正在运行，请等待它完成后再搜索",
            409,
        )
    selected_ids = _selected_work_directory_ids(
        db, workspace_id, work_directory_ids, include_root=include_root
    )
    search = AgentConversationSearch(
        workspace_id=workspace_id,
        query=normalized,
        work_directory_ids=selected_ids,
        include_root=True if selected_ids is None else include_root,
    )
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
        "work_directory_ids": search.work_directory_ids,
        "include_root": search.include_root,
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
            # Rendering durable search hits must remain on the same
            # deliberately low-priority Runtime path as the scan itself. It
            # must never spend an interactive hydration read slot.
            event = get_runtime().read_search_event(
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
    work_directory_ids = search.work_directory_ids
    include_root = search.include_root
    search.state = "RUNNING"
    db.commit()
    # Never retain a database transaction or row lock across unbounded native
    # EventLog scans. The delivery lease keeps this Worker task authoritative.
    try:
        workspace = conversations._workspace(db, workspace_id)
        statement = select(AgentConversationBinding).where(
            AgentConversationBinding.workspace_id == workspace_id,
            AgentConversationBinding.lifecycle == "ACTIVE",
        )
        if work_directory_ids is not None:
            scopes = []
            if include_root:
                scopes.append(AgentConversationBinding.work_directory_version_id.is_(None))
            if work_directory_ids:
                selected_versions = select(AgentWorkDirectoryVersion.id).join(
                    AgentWorkDirectory,
                    AgentWorkDirectory.id == AgentWorkDirectoryVersion.work_directory_id,
                ).where(
                    AgentWorkDirectory.workspace_id == workspace_id,
                    AgentWorkDirectory.id.in_(work_directory_ids),
                )
                scopes.append(
                    AgentConversationBinding.work_directory_version_id.in_(selected_versions)
                )
            statement = statement.where(or_(*scopes))
        bindings = list(db.scalars(statement.order_by(AgentConversationBinding.created_at.desc())))
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
