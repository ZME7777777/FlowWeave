from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class FlowRunRuntime(Base):
    """Stable logical Runtime Session for exactly one FlowRun."""

    __tablename__ = "flow_run_runtimes"
    __table_args__ = (
        CheckConstraint(
            "active_generation IS NULL OR active_generation >= 1",
            name="ck_flow_run_runtime_active_generation",
        ),
        CheckConstraint("row_version >= 1", name="ck_flow_run_runtime_row_version"),
        CheckConstraint("runtime_image_digest <> ''", name="ck_flow_run_runtime_image_digest"),
        CheckConstraint(
            "status IN ('STARTING', 'ACTIVE', 'REPLACING', 'RECONNECTING', "
            "'DEGRADED', 'STOPPED', 'DELETING')",
            name="ck_flow_run_runtime_status",
        ),
        ForeignKeyConstraint(
            ["id", "active_generation"],
            ["runtime_generations.runtime_session_id", "runtime_generations.generation"],
            name="fk_flow_run_runtime_active_generation",
            use_alter=True,
        ),
    )

    # The primary key is the stable Runtime Session identity. Physical
    # containers and endpoints live in generation/provider records instead.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(
        ForeignKey("flow_runs.id", ondelete="RESTRICT"), unique=True, index=True
    )
    environment_version_id: Mapped[str] = mapped_column(
        ForeignKey("environment_versions.id", ondelete="RESTRICT"), index=True
    )
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    workspace_allocation_id: Mapped[str] = mapped_column(
        ForeignKey("flow_run_runtime_allocations.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    active_generation: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="STARTING", index=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeGeneration(Base):
    """Immutable generation identity plus mutable fenced lifecycle facts."""

    __tablename__ = "runtime_generations"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id",
            "generation",
            name="uq_runtime_generation_session_number",
        ),
        CheckConstraint("generation >= 1", name="ck_runtime_generation_number"),
        CheckConstraint("row_version >= 1", name="ck_runtime_generation_row_version"),
        CheckConstraint("runtime_image_digest <> ''", name="ck_runtime_generation_image_digest"),
        CheckConstraint(
            "state IN ('PROVISIONING', 'READY', 'DRAINING', 'STOPPED', 'DELETED', 'FAILED')",
            name="ck_runtime_generation_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    runtime_session_id: Mapped[str] = mapped_column(
        ForeignKey("flow_run_runtimes.id", ondelete="RESTRICT"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    # ManagedSandbox is a replaceable physical-provider record. SET NULL keeps
    # the generation audit identity intact after that physical record is gone.
    managed_runtime_id: Mapped[str | None] = mapped_column(
        ForeignKey("managed_sandboxes.id", ondelete="SET NULL"), unique=True, index=True
    )
    instance_id: Mapped[str | None] = mapped_column(String(100))
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(30), default="PROVISIONING", index=True)
    fence_token: Mapped[str] = mapped_column(String(36), unique=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draining_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


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
    "FlowRunRuntime",
    "FlowRunRuntimeAllocation",
    "FlowRunRuntimeSecretReference",
    "ManagedSandbox",
    "RuntimeGeneration",
)
