from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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


class AgentWorkspace(Base):
    """A durable, Flow-independent workspace for direct Agent conversations."""

    __tablename__ = "agent_workspaces"
    __table_args__ = (
        CheckConstraint(
            "desired_state IN ('RUNNING', 'MAINTENANCE')",
            name="ck_agent_workspace_desired_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scope_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="Agent 工作区")
    desired_state: Mapped[str] = mapped_column(String(20), default="RUNNING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentWorkspaceRuntimeSecretReference(Base):
    __tablename__ = "agent_workspace_runtime_secret_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), unique=True, index=True
    )
    encrypted_secret_key: Mapped[bytes] = mapped_column(LargeBinary)
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentWorkspaceRuntimeAllocation(Base):
    __tablename__ = "agent_workspace_runtime_allocations"
    __table_args__ = (
        CheckConstraint(
            "relative_root LIKE '.agent-workspaces/%'",
            name="ck_agent_workspace_runtime_allocation_root",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), unique=True, index=True
    )
    secret_reference_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspace_runtime_secret_references.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    relative_root: Mapped[str] = mapped_column(String(500), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentWorkspaceRuntime(Base):
    __tablename__ = "agent_workspace_runtimes"
    __table_args__ = (
        CheckConstraint(
            "active_generation IS NULL OR active_generation >= 1",
            name="ck_agent_workspace_runtime_generation",
        ),
        CheckConstraint("row_version >= 1", name="ck_agent_workspace_runtime_row_version"),
        CheckConstraint(
            "runtime_image_digest <> ''", name="ck_agent_workspace_runtime_image_digest"
        ),
        CheckConstraint(
            "status IN ('STARTING', 'ACTIVE', 'RECONNECTING', 'DEGRADED', 'MAINTENANCE')",
            name="ck_agent_workspace_runtime_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), unique=True, index=True
    )
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    workspace_allocation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspace_runtime_allocations.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    active_generation: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="STARTING", index=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentWorkspaceRuntimeGeneration(Base):
    __tablename__ = "agent_workspace_runtime_generations"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id", "generation", name="uq_agent_workspace_runtime_generation"
        ),
        CheckConstraint("generation >= 1", name="ck_agent_workspace_runtime_generation_number"),
        CheckConstraint(
            "row_version >= 1", name="ck_agent_workspace_runtime_generation_row_version"
        ),
        CheckConstraint(
            "state IN ('PROVISIONING', 'READY', 'STOPPED', 'DELETED', 'FAILED')",
            name="ck_agent_workspace_runtime_generation_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    runtime_session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspace_runtimes.id", ondelete="RESTRICT"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    managed_runtime_id: Mapped[str | None] = mapped_column(
        ForeignKey("managed_sandboxes.id", ondelete="SET NULL"), unique=True, index=True
    )
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(20), default="PROVISIONING", index=True)
    fence_token: Mapped[str] = mapped_column(String(36), unique=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = (
    "AgentWorkspace",
    "AgentWorkspaceRuntime",
    "AgentWorkspaceRuntimeAllocation",
    "AgentWorkspaceRuntimeGeneration",
    "AgentWorkspaceRuntimeSecretReference",
)
