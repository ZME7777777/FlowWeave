"""Shared Agent-session locator and projection persistence mappings.

The table names deliberately remain stable while the default Agent Workspace
keeps compatibility imports.  A session binding belongs to the shared session
product; a product host only supplies its authorized host context and Runtime.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


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
            "working_directory IS NULL OR working_directory = '/runtime/workspace/project' "
            "OR working_directory LIKE '/runtime/workspace/project/%'",
            name="ck_agent_conversation_working_directory",
        ),
        CheckConstraint(
            "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'DELETED', 'FAILED')",
            name="ck_agent_conversation_lifecycle",
        ),
        CheckConstraint(
            "title_state IN ('PENDING', 'GENERATED', 'MANUAL', 'FALLBACK')",
            name="ck_agent_conversation_title_state",
        ),
        CheckConstraint("title_generation >= 1", name="ck_agent_conversation_title_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), index=True
    )
    runtime_session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_workspace_runtimes.id", ondelete="RESTRICT"), index=True
    )
    work_directory_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_work_directory_versions.id", ondelete="RESTRICT"), index=True
    )
    working_directory: Mapped[str | None] = mapped_column(String(500))
    model_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT"), index=True
    )
    model_name: Mapped[str | None] = mapped_column(String(240))
    reasoning_effort: Mapped[str | None] = mapped_column(String(30))
    streaming_callback_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    openhands_conversation_id: Mapped[str] = mapped_column(String(36))
    display_title: Mapped[str | None] = mapped_column(String(240))
    title_state: Mapped[str] = mapped_column(String(20), default="FALLBACK")
    title_generation: Mapped[int] = mapped_column(Integer, default=1)
    lifecycle: Mapped[str] = mapped_column(String(20), default="PROVISIONING", index=True)
    create_idempotency_key: Mapped[str] = mapped_column(String(200))
    bootstrap_parent_event_id: Mapped[str | None] = mapped_column(String(200))
    initial_user_event_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentConversationMessageAttachment(Base):
    """A display projection for files attached to a formal user MessageEvent."""

    __tablename__ = "agent_conversation_message_attachments"
    __table_args__ = (
        UniqueConstraint(
            "binding_id", "event_id", "path", name="uq_agent_conversation_message_attachment"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversation_bindings.id", ondelete="RESTRICT"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(200), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(240))
    mime_type: Mapped[str] = mapped_column(String(200))
    byte_size: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentConversationCapability(Base):
    """Immutable capability-version provenance frozen for one conversation."""

    __tablename__ = "agent_conversation_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "binding_id", "capability_version_id", name="uq_agent_conversation_capability"
        ),
        UniqueConstraint(
            "binding_id", "position", name="uq_agent_conversation_capability_position"
        ),
        CheckConstraint(
            "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
            name="ck_agent_conversation_capability_type",
        ),
        CheckConstraint("position >= 0", name="ck_agent_conversation_capability_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversation_bindings.id", ondelete="RESTRICT"), index=True
    )
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    capability_type: Mapped[str] = mapped_column(String(20))
    capability_key: Mapped[str] = mapped_column(String(160))
    digest: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
    "AgentConversationBinding",
    "AgentConversationCapability",
    "AgentConversationCommand",
    "AgentConversationMessageAttachment",
)
