"""Durable, user-owned batch deletion with a checkpoint per conversation."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentSidebarConversation,
)
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.modules.users.application.security import current_user_id
from flowweave.shared.application.transactions import run_commit_actions, savepoint
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState

TASK_TYPE = "DELETE_MANAGED_AGENT_CONVERSATIONS"
ACTIVE = (TaskState.PENDING, TaskState.RUNNING, TaskState.RETRY)


def _owned(workspace_id: str):
    # BackgroundTask is a global Worker ledger, without automatic tenant filters.
    return (
        BackgroundTask.owner_user_id == current_user_id(),
        BackgroundTask.task_type == TASK_TYPE,
        BackgroundTask.aggregate_id == workspace_id,
    )


def _public(task: BackgroundTask) -> dict[str, Any]:
    payload = task.payload_json
    binding_ids = payload["binding_ids"]
    deleted = payload.get("deleted_ids", [])
    skipped = payload.get("skipped", [])
    done = set(deleted) | {item["id"] for item in skipped}
    return {
        "id": task.id,
        "workspace_id": task.aggregate_id,
        "work_directory_id": payload.get("work_directory_id"),
        "workspace_label": payload["workspace_label"],
        "state": task.state,
        "total": len(binding_ids),
        "processed": len(done),
        "binding_ids": binding_ids,
        "deleted_ids": deleted,
        "skipped": skipped,
        "remaining_ids": [binding_id for binding_id in binding_ids if binding_id not in done],
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def listing(db: Session, workspace_id: str) -> dict[str, Any]:
    conversations._workspace(db, workspace_id)
    tasks = db.scalars(
        select(BackgroundTask)
        .where(*_owned(workspace_id))
        .order_by(BackgroundTask.created_at.desc(), BackgroundTask.id.desc())
        .limit(10)
    )
    return {"items": [_public(task) for task in tasks]}


def start(
    db: Session,
    workspace_id: str,
    binding_ids: tuple[str, ...],
    idempotency_key: str,
    *,
    work_directory_id: str | None = None,
) -> dict[str, Any]:
    conversations._workspace(db, workspace_id)
    requested = list(dict.fromkeys(binding_ids))
    if not requested or len(requested) > 500:
        raise DomainError("AGENT_DELETION_SELECTION_INVALID", "每次请选择1至500个会话", 422)
    work_directories._lock_directory_paths(db, workspace_id)
    key = (
        "managed-conversation-delete:"
        + hashlib.sha256(f"{workspace_id}:{idempotency_key}".encode()).hexdigest()
    )
    existing = db.scalar(
        select(BackgroundTask).where(*_owned(workspace_id), BackgroundTask.idempotency_key == key)
    )
    if existing:
        if (
            existing.payload_json["binding_ids"] != requested
            or existing.payload_json.get("work_directory_id") != work_directory_id
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "删除请求与原任务不一致", 409)
        return _public(existing)
    if db.scalar(
        select(BackgroundTask.id).where(*_owned(workspace_id), BackgroundTask.state.in_(ACTIVE))
    ):
        raise DomainError("AGENT_DELETION_IN_PROGRESS", "已有后台删除任务，请等待当前任务完成", 409)
    label = "根工作区"
    if work_directory_id:
        label = work_directories._directory(db, workspace_id, work_directory_id).display_name
    allowed = set(
        db.scalars(
            select(AgentConversationBinding.id).where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
                ~AgentConversationBinding.id.in_(
                    select(AgentSidebarConversation.sidebar_binding_id)
                ),
                conversations._management_conversation_scope(workspace_id, work_directory_id),
                AgentConversationBinding.id.in_(requested),
            )
        )
    )
    if set(requested) != allowed:
        raise DomainError(
            "AGENT_DELETION_SELECTION_CHANGED", "所选会话已变化，请刷新列表后重试", 409
        )
    task = enqueue(
        db,
        task_type=TASK_TYPE,
        aggregate_type="AGENT_CONVERSATION_DELETION",
        aggregate_id=workspace_id,
        idempotency_key=key,
        payload={
            "binding_ids": requested,
            "work_directory_id": work_directory_id,
            "workspace_label": label,
            "deleted_ids": [],
            "skipped": [],
        },
    )
    return _public(task)


def process(db: Session, workspace_id: str, lease: Lease) -> None:
    task = db.scalar(
        select(BackgroundTask).where(*_owned(workspace_id), BackgroundTask.id == lease.task_id)
    )
    if task is None:
        raise DomainError("AGENT_DELETION_NOT_FOUND", "删除任务不存在或不可访问", 404)
    # No task-row lock is held over Runtime calls: heartbeat and UI progress
    # reads must remain independent. A fenced update settles each item.
    payload = dict(task.payload_json)
    completed = set(payload.get("deleted_ids", [])) | {
        item["id"] for item in payload.get("skipped", [])
    }
    for binding_id in payload["binding_ids"]:
        if binding_id in completed:
            continue
        if not lease_is_current(db, lease):
            raise DomainError("AGENT_DELETION_LEASE_LOST", "删除任务已由其他进程接管", 409)
        try:
            with savepoint(db):
                # Re-authorize the exact saved user/directory selection for
                # every item; this also checks native readiness before delete.
                result = conversations.delete_management_conversations(
                    db,
                    workspace_id,
                    (binding_id,),
                    f"batch-job:{lease.task_id}",
                    work_directory_id=payload.get("work_directory_id"),
                )
                # The legacy synchronous helper returns domain failures as
                # skips. Transient failures must retry without committing a
                # DELETE_PENDING locator or losing prior checkpoints.
                skipped = result["skipped"]
                if skipped and skipped[0]["code"] not in {
                    "AGENT_CONVERSATION_RUNNING",
                    "AGENT_CONVERSATION_SCOPE_MISMATCH",
                    "AGENT_CONVERSATION_NOT_FOUND",
                }:
                    raise DomainError(
                        "AGENT_DELETION_RUNTIME_RETRY", "暂时无法删除会话，后台稍后重试", 503
                    )
        except BaseException:
            db.rollback()
            raise
        payload = {
            **payload,
            "deleted_ids": [*payload.get("deleted_ids", []), *result["deleted_ids"]],
            "skipped": [*payload.get("skipped", []), *result["skipped"]],
        }
        updated = db.execute(
            update(BackgroundTask)
            .where(
                *_owned(workspace_id),
                BackgroundTask.id == lease.task_id,
                BackgroundTask.state == TaskState.RUNNING,
                BackgroundTask.lease_owner == lease.owner,
                BackgroundTask.lease_generation == lease.generation,
                BackgroundTask.lease_until >= now(),
            )
            .values(payload_json=payload, updated_at=now())
        )
        if updated.rowcount != 1:
            db.rollback()
            raise DomainError("AGENT_DELETION_LEASE_LOST", "删除任务租约已失效", 409)
        # Each native deletion and its locator/result settle together. A
        # Worker restart resumes the remainder with the same stable item key.
        db.commit()
        run_commit_actions(db)
        completed.add(binding_id)
