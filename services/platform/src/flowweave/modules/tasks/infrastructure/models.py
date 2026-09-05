from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid
from flowweave.shared.domain.enums import TaskState


class BackgroundTask(Base):
    __tablename__ = "background_tasks"
    # Workers must scan this delivery ledger globally, then restore the task's
    # tenant before touching its aggregate. It therefore carries an explicit
    # owner but is not subject to ordinary ORM/RLS filtering.
    __tenant_scoped__: ClassVar[bool] = False
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_background_task_owner_key"
        ),
        CheckConstraint("lease_generation >= 0", name="ck_task_generation_nonnegative"),
        CheckConstraint("attempts >= 0", name="ck_task_attempts_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_user_id: Mapped[str] = mapped_column(String(36), index=True)
    task_type: Mapped[str] = mapped_column(String(60), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(20), default=TaskState.PENDING, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ("BackgroundTask",)
