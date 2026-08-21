from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class FlowRunRuntimeSecretReference(Base):
    """Stable encrypted reference for one FlowRun's OpenHands secret key."""

    __tablename__ = "flow_run_runtime_secret_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    encrypted_secret_key: Mapped[bytes] = mapped_column(LargeBinary)
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FlowRunRuntimeAllocation(Base):
    """Server-derived external storage allocated for exactly one FlowRun."""

    __tablename__ = "flow_run_runtime_allocations"
    __table_args__ = (
        CheckConstraint(
            "relative_root LIKE '.flow-run-runtimes/%'",
            name="ck_flow_run_runtime_allocation_root",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(
        ForeignKey("flow_runs.id", ondelete="RESTRICT"), unique=True, index=True
    )
    secret_reference_id: Mapped[str] = mapped_column(
        ForeignKey("flow_run_runtime_secret_references.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    relative_root: Mapped[str] = mapped_column(String(500), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
    runtime_allocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("flow_run_runtime_allocations.id", ondelete="RESTRICT"), index=True
    )
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


__all__ = (
    "FlowRunRuntimeAllocation",
    "FlowRunRuntimeSecretReference",
    "ManagedSandbox",
)
