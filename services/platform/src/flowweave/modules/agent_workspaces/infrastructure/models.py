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

from flowweave.modules.agent_sessions.public import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)
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
    default_model_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT"), index=True
    )
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


class AgentWorkDirectory(Base):
    """A named product grouping over one or more project-root subdirectories."""

    __tablename__ = "agent_work_directories"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "display_name", name="uq_agent_work_directory_workspace_name"
        ),
        UniqueConstraint(
            "flow_run_id",
            "node_attempt_id",
            "display_name",
            name="uq_agent_work_directory_flow_run_attempt_name",
        ),
        CheckConstraint(
            "(workspace_id IS NOT NULL AND flow_run_id IS NULL AND node_attempt_id IS NULL) "
            "OR (workspace_id IS NULL AND flow_run_id IS NOT NULL)",
            name="ck_agent_work_directory_owner",
        ),
        CheckConstraint("state IN ('ACTIVE', 'ARCHIVED')", name="ck_agent_work_directory_state"),
        CheckConstraint("current_version >= 1", name="ck_agent_work_directory_current_version"),
        CheckConstraint("row_version >= 1", name="ck_agent_work_directory_row_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), index=True
    )
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentWorkDirectoryVersion(Base):
    """An immutable path selection that future Conversations can freeze."""

    __tablename__ = "agent_work_directory_versions"
    __table_args__ = (
        UniqueConstraint("work_directory_id", "version", name="uq_agent_work_directory_version"),
        CheckConstraint("version >= 1", name="ck_agent_work_directory_version_number"),
        CheckConstraint(
            "working_path = '.' OR (working_path <> '' AND working_path NOT LIKE '/%')",
            name="ck_agent_work_directory_working_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    work_directory_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    working_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentWorkDirectoryPath(Base):
    __tablename__ = "agent_work_directory_paths"
    __table_args__ = (
        UniqueConstraint(
            "version_id", "relative_path", name="uq_agent_work_directory_version_path"
        ),
        UniqueConstraint("version_id", "position", name="uq_agent_work_directory_version_position"),
        CheckConstraint("position >= 0", name="ck_agent_work_directory_path_position"),
        CheckConstraint(
            "relative_path <> '' AND relative_path NOT LIKE '/%'",
            name="ck_agent_work_directory_relative_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    relative_path: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer)


class AgentWorkspaceCapability(Base):
    """A governed capability enabled for new Agent Workspace conversations."""

    __tablename__ = "agent_workspace_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "capability_version_id", name="uq_agent_workspace_capability"
        ),
        UniqueConstraint("workspace_id", "position", name="uq_agent_workspace_capability_position"),
        CheckConstraint(
            "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
            name="ck_agent_workspace_capability_type",
        ),
        CheckConstraint("position >= 0", name="ck_agent_workspace_capability_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), index=True
    )
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    capability_type: Mapped[str] = mapped_column(String(20))
    capability_key: Mapped[str] = mapped_column(String(160))
    digest: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = (
    "AgentWorkspace",
    "AgentConversationBinding",
    "AgentConversationCapability",
    "AgentWorkspaceCapability",
    "AgentConversationMessageAttachment",
    "AgentConversationCommand",
    "AgentWorkDirectory",
    "AgentWorkDirectoryPath",
    "AgentWorkDirectoryVersion",
    "AgentWorkspaceRuntime",
    "AgentWorkspaceRuntimeAllocation",
    "AgentWorkspaceRuntimeGeneration",
    "AgentWorkspaceRuntimeSecretReference",
)
