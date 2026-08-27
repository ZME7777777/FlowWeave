from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class AgentConversationBinding(Base):
    __tablename__ = "agent_conversation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id",
            "openhands_conversation_id",
            name="uq_agent_conversation_runtime_id",
        ),
        UniqueConstraint("create_idempotency_key", name="uq_agent_conversation_create_key"),
        CheckConstraint(
            "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'DELETED', 'FAILED')",
            name="ck_agent_conversation_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), index=True
    )
    runtime_session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspace_runtimes.id", ondelete="RESTRICT"), index=True
    )
    # This is frozen when the native OpenHands conversation is created.  It is
    # intentionally nullable for pre-FR-29 bindings: their original provider
    # was not persisted and must not be guessed during migration.
    model_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT"), index=True
    )
    # True only when the Event Service was created from an LLM with stream=True,
    # which makes OpenHands 1.42.0 attach its formal token callback. Historical
    # rows are migrated as False because switch_llm cannot add that callback.
    streaming_callback_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    openhands_conversation_id: Mapped[str] = mapped_column(String(36))
    display_title: Mapped[str | None] = mapped_column(String(240))
    lifecycle: Mapped[str] = mapped_column(String(20), default="PROVISIONING", index=True)
    create_idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentConversationCommand(Base):
    __tablename__ = "agent_conversation_commands"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_agent_conversation_command_key"),
        CheckConstraint(
            "command_type IN ('CREATE', 'DELETE', 'RENAME', 'FORK')", name="ck_agent_command_type"
        ),
        CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'AMBIGUOUS', 'FAILED')",
            name="ck_agent_command_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), index=True
    )
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversation_bindings.id", ondelete="RESTRICT"), index=True
    )
    command_type: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = (
    "AgentWorkspace",
    "AgentConversationBinding",
    "AgentConversationCommand",
    "AgentWorkspaceRuntime",
    "AgentWorkspaceRuntimeAllocation",
    "AgentWorkspaceRuntimeGeneration",
    "AgentWorkspaceRuntimeSecretReference",
)
