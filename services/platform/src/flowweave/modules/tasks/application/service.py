from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_user_id
from flowweave.shared.models import BackgroundTask, TaskState


@dataclass(frozen=True, slots=True)
class Lease:
    task_id: str
    owner: str
    generation: int


def enqueue(
    db: Session,
    *,
    task_type: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    available_at: datetime | None = None,
) -> BackgroundTask:
    owner_user_id = current_user_id(default=FLOWWEAVE_USER_ID)
    existing = db.scalar(
        select(BackgroundTask).where(
            BackgroundTask.owner_user_id == owner_user_id,
            BackgroundTask.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    task = BackgroundTask(
        owner_user_id=owner_user_id,
        task_type=task_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload_json=payload or {},
        available_at=available_at or datetime.now(UTC),
    )
    db.add(task)
    db.flush()
    return task


def recover_expired(db: Session, *, commit: bool = True) -> int:
    now = datetime.now(UTC)
    result = db.execute(
        update(BackgroundTask)
        .where(
            BackgroundTask.state == TaskState.RUNNING,
            BackgroundTask.lease_until < now,
        )
        .values(
            state=TaskState.RETRY,
            lease_owner=None,
            lease_until=None,
            available_at=now,
            last_error="LEASE_EXPIRED",
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return result.rowcount


def claim(
    db: Session, owner: str, *, lease_seconds: int, commit: bool = True
) -> tuple[BackgroundTask, Lease] | None:
    now = datetime.now(UTC)
    stmt = (
        select(BackgroundTask)
        .where(
            BackgroundTask.state.in_([TaskState.PENDING, TaskState.RETRY]),
            BackgroundTask.available_at <= now,
        )
        .order_by(BackgroundTask.available_at, BackgroundTask.created_at)
        .limit(1)
    )
    task = db.scalar(stmt.with_for_update(skip_locked=True))
    if not task:
        return None
    task.state = TaskState.RUNNING
    task.lease_owner = owner
    task.lease_until = now + timedelta(seconds=lease_seconds)
    task.lease_generation += 1
    task.attempts += 1
    if commit:
        db.commit()
    else:
        db.flush()
    return task, Lease(task.id, owner, task.lease_generation)


def _lease_filter(lease: Lease):
    return (
        BackgroundTask.id == lease.task_id,
        BackgroundTask.state == TaskState.RUNNING,
        BackgroundTask.lease_owner == lease.owner,
        BackgroundTask.lease_generation == lease.generation,
    )


def lease_is_current(db: Session, lease: Lease) -> bool:
    return (
        db.scalar(
            select(BackgroundTask.id).where(
                *_lease_filter(lease),
                BackgroundTask.lease_until >= datetime.now(UTC),
            )
        )
        is not None
    )


def heartbeat(db: Session, lease: Lease, *, lease_seconds: int, commit: bool = True) -> bool:
    result = db.execute(
        update(BackgroundTask)
        .where(
            *_lease_filter(lease),
            BackgroundTask.lease_until >= datetime.now(UTC),
        )
        .values(lease_until=datetime.now(UTC) + timedelta(seconds=lease_seconds))
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return result.rowcount == 1


def succeed(db: Session, lease: Lease, *, commit: bool = True) -> bool:
    result = db.execute(
        update(BackgroundTask)
        .where(*_lease_filter(lease), BackgroundTask.lease_until >= datetime.now(UTC))
        .values(state=TaskState.SUCCEEDED, lease_owner=None, lease_until=None)
    )
    if result.rowcount != 1:
        if commit:
            db.rollback()
        return False
    if commit:
        db.commit()
    else:
        db.flush()
    return True


def fail(
    db: Session,
    lease: Lease,
    error: str,
    *,
    permanent: bool = False,
    commit: bool = True,
) -> bool:
    task = db.scalar(
        select(BackgroundTask).where(
            *_lease_filter(lease),
            BackgroundTask.lease_until >= datetime.now(UTC),
        )
    )
    if not task:
        if commit:
            db.rollback()
        return False
    task.state = (
        TaskState.DEAD if permanent or task.attempts >= task.max_attempts else TaskState.RETRY
    )
    task.available_at = datetime.now(UTC) + timedelta(seconds=min(2**task.attempts, 60))
    task.lease_owner = None
    task.lease_until = None
    task.last_error = error[:2000]
    if commit:
        db.commit()
    else:
        db.flush()
    return True
