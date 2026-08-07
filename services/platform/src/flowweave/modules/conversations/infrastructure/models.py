from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.modules.conversations.domain.enums import ConversationState
from flowweave.shared.database import Base, now, uid


class AgentConversation(Base):
    __tablename__ = "agent_conversations"
    __table_args__ = (
        Index("uq_agent_conversation_number", "attempt_id", "conversation_no", unique=True),
        Index(
            "uq_agent_conversation_auto",
            "attempt_id",
            unique=True,
            postgresql_where=text("kind = 'AUTO'"),
        ),
        CheckConstraint("next_sequence_no > 0", name="ck_conversation_next_sequence_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(30), default=ConversationState.CREATING)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    runtime_adapter: Mapped[str | None] = mapped_column(String(30))
    runtime_job_id: Mapped[str | None] = mapped_column(String(100))
    runtime_conversation_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    runtime_cursor: Mapped[str | None] = mapped_column(String(200))
    context_baseline_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_sequence_no: Mapped[int] = mapped_column(BigInteger, default=1)
    created_by_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("uq_agent_message_sequence", "conversation_id", "sequence_no", unique=True),
        Index(
            "uq_agent_message_client_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        Index(
            "uq_agent_message_runtime_event",
            "conversation_id",
            "runtime_event_id",
            unique=True,
            postgresql_where=text("runtime_event_id IS NOT NULL"),
        ),
        Index("ix_agent_message_delivery", "delivery_state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    sequence_no: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(20))
    transport_role: Mapped[str] = mapped_column(String(20))
    message_type: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    delivery_state: Mapped[str] = mapped_column(String(20))
    delivery_mode: Mapped[str | None] = mapped_column(String(30))
    client_message_id: Mapped[str | None] = mapped_column(String(100))
    runtime_event_id: Mapped[str | None] = mapped_column(String(200))
    runtime_cursor: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageArtifactRef(Base):
    __tablename__ = "message_artifact_refs"

    message_id: Mapped[str] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="CASCADE"), primary_key=True
    )
    artifact_version_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ("AgentConversation", "AgentMessage", "MessageArtifactRef")
