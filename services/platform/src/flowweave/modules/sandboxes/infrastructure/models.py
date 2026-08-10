from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class ManagedSandbox(Base):
    """Durable desired and observed state for one isolated compute resource."""

    __tablename__ = "managed_sandboxes"
    __table_args__ = (
        UniqueConstraint("backend", "backend_resource_name", name="uq_sandbox_backend_name"),
        UniqueConstraint(
            "kind",
            "owner_type",
            "owner_id",
            "generation",
            name="uq_sandbox_owner_generation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    owner_type: Mapped[str] = mapped_column(String(40), index=True)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    backend: Mapped[str] = mapped_column(String(30), default="docker")
    backend_resource_id: Mapped[str] = mapped_column(String(100), default="")
    backend_resource_name: Mapped[str] = mapped_column(String(100))
    desired_state: Mapped[str] = mapped_column(String(20), default="RUNNING", index=True)
    observed_state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    image_reference: Mapped[str] = mapped_column(String(500))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    idle_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_reconcile_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ("ManagedSandbox",)
