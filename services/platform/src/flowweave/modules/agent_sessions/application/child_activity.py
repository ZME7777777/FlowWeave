"""Parent-scoped projections. Native activity and each child's unread stay separate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.activity_reconciliation import (
    read_activity_completion,
)
from flowweave.modules.agent_sessions.application.child_creation import POLICY_VERSION
from flowweave.modules.agent_sessions.application.child_relations import guard_main_route
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_user_id
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.runtime.base import RuntimeConversationActivity
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.domain.event_monitoring import activity_timestamp_is_stale
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState

_RUNNING = frozenset({"starting", "running", "executing", "stopping", "waiting_for_confirmation"})


def parent(db: Session, parent_id: str) -> AgentConversationBinding:
    owner = current_user_id()
    binding = db.get(AgentConversationBinding, parent_id)
    if (
        agent_session_principal(db, owner) is None
        or binding is None
        or binding.owner_user_id != owner
        or binding.lifecycle not in {"ACTIVE", "DELETE_PENDING"}
    ):
        raise DomainError("AGENT_CHILD_NOT_AVAILABLE", "父会话不存在或不可访问", 404)
    return guard_main_route(
        db, host_kind=binding.host_kind, host_id=binding.host_id, binding_id=binding.id
    )


def _children(
    db: Session, parents: Sequence[AgentConversationBinding], *, lock: bool = False
) -> list[tuple[AgentConversationBinding, AgentConversationBinding]]:
    owner = current_user_id(default=FLOWWEAVE_USER_ID)
    by_id = {item.id: item for item in parents if item.owner_user_id == owner}
    if not by_id:
        return []
    query = (
        select(AgentChildConversation, AgentConversationBinding)
        .join(
            AgentConversationBinding,
            AgentConversationBinding.id == AgentChildConversation.child_binding_id,
        )
        .where(
            AgentChildConversation.parent_binding_id.in_(by_id),
            AgentChildConversation.owner_user_id == owner,
            AgentConversationBinding.owner_user_id == owner,
            AgentChildConversation.creation_policy_version == POLICY_VERSION,
        )
        .order_by(AgentConversationBinding.created_at, AgentConversationBinding.id)
    )
    if lock:
        query = query.with_for_update(of=AgentConversationBinding)
    children: list[tuple[AgentConversationBinding, AgentConversationBinding]] = []
    for relation, child in db.execute(query).all():
        main = by_id[relation.parent_binding_id]
        if (
            relation.parent_conversation_id == main.openhands_conversation_id
            and relation.child_conversation_id == child.openhands_conversation_id
            and child.node_attempt_id is None
            and child.node_run_id is None
            and child.working_directory == main.working_directory
            and (
                relation.host_kind,
                relation.host_id,
                relation.conversation_scope_id,
                relation.runtime_session_id,
            )
            == (main.host_kind, main.host_id, main.conversation_scope_id, main.runtime_session_id)
            == (
                child.host_kind,
                child.host_id,
                child.conversation_scope_id,
                child.runtime_session_id,
            )
        ):
            children.append((main, child))
    return children


def summaries(
    db: Session, parents: Sequence[AgentConversationBinding]
) -> dict[str, dict[str, Any]]:
    """Lists read only control-plane metadata, leaving native counters unknown."""
    result: dict[str, dict[str, Any]] = {
        item.id: {
            "total": 0,
            "unread": 0,
            "running": None,
            "attention": None,
            "condensing": None,
            "unknown": None,
            "pending": None,
        }
        for item in parents
    }
    for main, child in _children(db, parents):
        result[main.id]["total"] += 1
        result[main.id]["unread"] += int(child.unread)
    return result


def list_children(db: Session, parent_id: str) -> list[dict[str, Any]]:
    """Closing a view has no lifecycle effect. No native I/O or TTL is involved."""
    from flowweave.modules.agent_sessions.application.conversations import final_reply_preview

    return [
        {
            "id": child.id,
            "parent_binding_id": main.id,
            "display_title": child.display_title,
            "lifecycle": child.lifecycle,
            "unread": child.unread,
            "unread_origin": child.unread_origin,
            "final_reply_preview": final_reply_preview(child),
            "created_at": child.created_at.isoformat(),
        }
        for main, child in _children(db, [parent(db, parent_id)])
    ]


def set_unread(
    db: Session, parent_id: str, child_id: str, *, unread: bool, origin: str | None
) -> dict[str, Any]:
    if origin not in {None, "MANUAL", "SYSTEM"}:
        raise DomainError("AGENT_CONVERSATION_UNREAD_ORIGIN_INVALID", "未读来源无效", 422)
    target = next(
        (
            child
            for _, child in _children(db, [parent(db, parent_id)], lock=True)
            if child.id == child_id
        ),
        None,
    )
    if target is None:
        raise DomainError("AGENT_CHILD_NOT_AVAILABLE", "子会话不存在或不可访问", 404)
    target.unread = unread
    target.unread_origin = origin or ("MANUAL" if unread else None)
    target.final_reply_preview_event_id = None
    target.final_reply_preview_content = None
    db.flush()
    return {"id": target.id, "unread": target.unread, "unread_origin": target.unread_origin}


def reconcile(
    db: Session,
    parents: Sequence[AgentConversationBinding],
    native: Mapping[str, RuntimeConversationActivity],
    *,
    active_binding_id: str | None,
    active_child_id: str | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Reuse the host's one native catalog, reconciling only observed child turns.

    The host activity lane already serializes its database projection. Child
    rows share its transaction and lock with unread writes, so a delayed poll
    cannot overwrite an explicit acknowledgement using an older ORM snapshot.
    """
    from flowweave.modules.agent_sessions.application import child_sessions

    rows = _children(db, parents, lock=True)
    result: dict[str, dict[str, Any]] = {
        item.id: {
            "total": 0,
            "unread": 0,
            "running": 0,
            "attention": 0,
            "condensing": 0,
            "unknown": 0,
            "pending": 0,
        }
        for item in parents
    }
    states: list[dict[str, Any]] = []
    tasks: dict[str, str] = {}
    child_ids = [child.id for _, child in rows]
    if child_ids:
        for binding_id, state in db.execute(
            select(BackgroundTask.aggregate_id, BackgroundTask.state)
            .where(
                BackgroundTask.aggregate_id.in_(child_ids),
                BackgroundTask.task_type == "CONDENSE_AGENT_CONVERSATION",
                BackgroundTask.owner_user_id == current_user_id(default=FLOWWEAVE_USER_ID),
            )
            .order_by(BackgroundTask.created_at.desc(), BackgroundTask.id.desc())
        ):
            tasks.setdefault(binding_id, state)
    for main, child in rows:
        # Reload under the row lock, after a concurrent read acknowledgement.
        db.refresh(child)
        activity = native.get(child.openhands_conversation_id)
        status = "unknown"
        attention = False
        loc = None
        try:
            loc = child_sessions.resolve(db, main.id, child.id)
        except DomainError as exc:
            if exc.status not in {404, 409, 503, 504}:
                raise
        if loc is not None:
            status = activity.execution_status if activity is not None else "unknown"
            background = (main.id, child.id) != (active_binding_id, active_child_id)
            running = status in _RUNNING
            attention = status in {"error", "stuck"} or bool(
                running
                and activity is not None
                and activity_timestamp_is_stale(activity.updated_at)
            )
            if running:
                child.final_reply_preview_event_id = None
                child.final_reply_preview_content = None
                child.activity_was_running = True
                child.terminal_reconciliation_pending = False
            elif (
                child.activity_was_running
                or child.terminal_reconciliation_pending
                or (
                    activity is not None
                    and child.child_completion_checked_activity_at != activity.updated_at
                )
            ):
                # A missing catalog row is unknown. Only a formal native result
                # may close the pending turn or create a completion notification.
                child.terminal_reconciliation_pending = True
                try:
                    completion = read_activity_completion(get_runtime(), loc.handle)
                except DomainError as exc:
                    if exc.status not in {404, 409, 503, 504}:
                        raise
                    completion = None
                if completion is not None and completion.status in {
                    "COMPLETED",
                    "FAILED",
                    "PAUSED",
                }:
                    child.activity_was_running = False
                    child.terminal_reconciliation_pending = False
                    if activity is not None:
                        child.child_completion_checked_activity_at = activity.updated_at
                    if (
                        completion.status == "COMPLETED"
                        and completion.completion_event_kind == "ASSISTANT_MESSAGE"
                        and completion.completion_event_id
                        and completion.final_message
                        and child.last_notified_completion_event_id
                        != completion.completion_event_id
                    ):
                        child.last_notified_completion_event_id = completion.completion_event_id
                        if background:
                            child.unread = True
                            child.unread_origin = "MANUAL"
                            child.final_reply_preview_event_id = completion.completion_event_id
                            child.final_reply_preview_content = completion.final_message
            if attention and background:
                if not child.unread and child.unread_origin != "SYSTEM":
                    child.unread = True
                    child.unread_origin = "SYSTEM"
                    child.final_reply_preview_event_id = None
                    child.final_reply_preview_content = None
            elif activity is not None and not attention and not child.unread:
                if child.unread_origin == "SYSTEM":
                    child.unread_origin = None
        condensing = tasks.get(child.id) in {TaskState.PENDING, TaskState.RUNNING}
        summary = result[main.id]
        summary["total"] += 1
        summary["unread"] += int(child.unread)
        summary["running"] += int(status in _RUNNING)
        summary["attention"] += int(child.unread and child.unread_origin == "SYSTEM" and attention)
        summary["unknown"] += int(status == "unknown")
        pending = child.terminal_reconciliation_pending or (
            status == "unknown" and child.activity_was_running
        )
        summary["pending"] += int(pending)
        summary["condensing"] += int(condensing)
        states.append(
            {
                "id": child.id,
                "parent_binding_id": main.id,
                "execution_status": status,
                "possibly_stuck": attention and status in _RUNNING,
                "failed": status in {"error", "stuck"},
                "condensing": condensing,
                "sync_pending": pending,
                "unread": child.unread,
                "unread_origin": child.unread_origin,
            }
        )
    db.flush()
    return result, states
