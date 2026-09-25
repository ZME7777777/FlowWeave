"""Durable delivery of native OpenHands manual condensation requests."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.usage_reconciliation import resolve_handle
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState


def enqueue_manual_condensation(
    db: Session, *, binding_id: str, idempotency_key: str
) -> BackgroundTask:
    """Create or reuse the one active condensation task for a binding."""

    active = db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.task_type == "CONDENSE_AGENT_CONVERSATION",
            BackgroundTask.aggregate_id == binding_id,
            BackgroundTask.state.in_([TaskState.PENDING, TaskState.RUNNING]),
        )
        .order_by(BackgroundTask.created_at.desc())
    )
    if active is not None:
        return active
    task = enqueue(
        db,
        task_type="CONDENSE_AGENT_CONVERSATION",
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding_id,
        idempotency_key=idempotency_key,
    )
    task.max_attempts = 1
    db.flush()
    return task


def process_manual_condensation(
    db: Session, binding_id: str, _payload: dict[str, object], lease: Lease
) -> None:
    """Run one accepted native condensation without retaining a DB transaction."""

    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost before manual condensation")
    handle = resolve_handle(db, binding_id)
    db.rollback()
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError(
            "AGENT_CONVERSATION_BUSY",
            "请在当前回复完成或暂停后压缩上下文",
            409,
        )
    runtime.condense(handle)


__all__ = ("enqueue_manual_condensation", "process_manual_condensation")
