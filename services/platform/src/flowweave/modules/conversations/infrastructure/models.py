from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
    runtime_sandbox_id: Mapped[str | None] = mapped_column(String(36), index=True)
    fork_kind: Mapped[str | None] = mapped_column(String(20))
    source_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="SET NULL"), index=True
    )
    source_runtime_conversation_id: Mapped[str | None] = mapped_column(String(100))
    source_runtime_event_id: Mapped[str | None] = mapped_column(String(200))
    runtime_branch_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics_reset: Mapped[bool | None] = mapped_column(Boolean)
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


class RuntimeCondensation(Base):
    """Durable projection of one native OpenHands Condensation event."""

    __tablename__ = "runtime_condensations"
    __table_args__ = (
        Index(
            "uq_runtime_condensation_event",
            "conversation_id",
            "runtime_event_id",
            unique=True,
        ),
        Index("ix_runtime_condensation_attempt_created", "attempt_id", "created_at"),
        CheckConstraint(
            "summary_offset IS NULL OR summary_offset >= 0",
            name="ck_runtime_condensation_summary_offset",
        ),
        CheckConstraint(
            "event_type IN ('REQUESTED', 'COMPLETED')",
            name="ck_runtime_condensation_event_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    runtime_event_id: Mapped[str] = mapped_column(String(200))
    runtime_cursor: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(20))
    forgotten_event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_offset: Mapped[int | None] = mapped_column(Integer)
    llm_response_id: Mapped[str | None] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuntimeConversationFork(Base):
    """Durable command and identity ledger for one native Runtime fork."""

    __tablename__ = "runtime_conversation_forks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'FAILED')",
            name="ck_runtime_conversation_fork_state",
        ),
        CheckConstraint(
            "state_version > 0 AND source_state_version > 0",
            name="ck_runtime_conversation_fork_versions",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    source_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="SET NULL"), index=True
    )
    target_conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    runtime_adapter: Mapped[str] = mapped_column(String(30))
    runtime_job_id: Mapped[str] = mapped_column(String(100))
    runtime_sandbox_id: Mapped[str | None] = mapped_column(String(36))
    source_runtime_conversation_id: Mapped[str] = mapped_column(String(100))
    target_runtime_conversation_id: Mapped[str] = mapped_column(String(100), unique=True)
    requested_from_event_id: Mapped[str | None] = mapped_column(String(200))
    source_head_event_id: Mapped[str] = mapped_column(String(200))
    resolved_source_event_id: Mapped[str | None] = mapped_column(String(200))
    fork_leaf_event_id: Mapped[str | None] = mapped_column(String(200))
    reset_metrics: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    source_state_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RuntimeCondensationCommand(Base):
    """Durable control-plane command for one native manual condensation."""

    __tablename__ = "runtime_condensation_commands"
    __table_args__ = (
        Index(
            "uq_runtime_condensation_command_active",
            "conversation_id",
            unique=True,
            postgresql_where=text("state = 'PENDING'"),
        ),
        CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_runtime_condensation_command_state",
        ),
        CheckConstraint(
            "state_version > 0",
            name="ck_runtime_condensation_command_version_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    runtime_conversation_id: Mapped[str] = mapped_column(String(100))
    baseline_cursor: Mapped[str | None] = mapped_column(String(200))
    request_event_id: Mapped[str | None] = mapped_column(String(200))
    completion_event_id: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    requested_by: Mapped[str | None] = mapped_column(String(160))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RuntimeConfirmationBatch(Base):
    """Durable projection of one OpenHands pending-action batch."""

    __tablename__ = "runtime_confirmation_batches"
    __table_args__ = (
        Index(
            "uq_runtime_confirmation_pending",
            "conversation_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'DECIDING')"),
        ),
        Index("ix_runtime_confirmation_attempt_created", "attempt_id", "created_at"),
        CheckConstraint("action_count > 0", name="ck_runtime_confirmation_actions_positive"),
        CheckConstraint("state_version > 0", name="ck_runtime_confirmation_version_positive"),
        CheckConstraint(
            "state IN ('PENDING', 'DECIDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="ck_runtime_confirmation_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    runtime_conversation_id: Mapped[str] = mapped_column(String(100))
    runtime_cursor: Mapped[str | None] = mapped_column(String(200))
    pending_actions_digest: Mapped[str] = mapped_column(String(64))
    pending_actions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    risk_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    policy_version_id: Mapped[str | None] = mapped_column(String(36))
    action_count: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    decision_accept: Mapped[bool | None] = mapped_column(Boolean)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(180), unique=True)
    decided_by: Mapped[str | None] = mapped_column(String(160))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_response_cursor: Mapped[str | None] = mapped_column(String(200))
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RuntimeSubagentTask(Base):
    """Durable projection of one native OpenHands Task tool invocation.

    ``action_event_id`` is the stable invocation identity. OpenHands may reuse
    ``task_id`` when a task is resumed, so task ids are deliberately not unique.
    Observation events reference the invocation through their formal
    ``action_id`` field; no ordering or name-based correlation is used.
    """

    __tablename__ = "runtime_subagent_tasks"
    __table_args__ = (
        Index(
            "uq_runtime_subagent_task_action",
            "conversation_id",
            "action_event_id",
            unique=True,
        ),
        Index(
            "uq_runtime_subagent_task_observation",
            "conversation_id",
            "observation_event_id",
            unique=True,
            postgresql_where=text("observation_event_id IS NOT NULL"),
        ),
        Index(
            "ix_runtime_subagent_task_attempt_created",
            "attempt_id",
            "created_at",
        ),
        CheckConstraint(
            "state IN ('REQUESTED', 'COMPLETED', 'ERROR')",
            name="ck_runtime_subagent_task_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    action_event_id: Mapped[str] = mapped_column(String(200))
    action_cursor: Mapped[str | None] = mapped_column(String(200))
    tool_call_id: Mapped[str | None] = mapped_column(String(200), index=True)
    llm_response_id: Mapped[str | None] = mapped_column(String(200), index=True)
    observation_event_id: Mapped[str | None] = mapped_column(String(200))
    observation_cursor: Mapped[str | None] = mapped_column(String(200))
    runtime_task_id: Mapped[str | None] = mapped_column(String(100), index=True)
    subagent_type: Mapped[str] = mapped_column(String(200), default="unknown")
    description: Mapped[str | None] = mapped_column(Text)
    resume_task_id: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="REQUESTED", index=True)
    native_status: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RuntimeSubagentTaskUsage(Base):
    """One cumulative metrics ledger row per native OpenHands Task identity."""

    __tablename__ = "runtime_subagent_task_usage"
    __table_args__ = (
        Index(
            "uq_runtime_subagent_task_usage_identity",
            "conversation_id",
            "runtime_task_id",
            unique=True,
        ),
        CheckConstraint("usage_version > 0", name="ck_runtime_subagent_usage_version"),
        CheckConstraint(
            "accumulated_cost_usd >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND reasoning_tokens >= 0 "
            "AND context_window >= 0 AND per_turn_tokens >= 0",
            name="ck_runtime_subagent_usage_nonnegative",
        ),
        CheckConstraint(
            "budget_state IN ('UNBOUNDED', 'WITHIN', 'EXCEEDED')",
            name="ck_runtime_subagent_usage_budget_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    runtime_subagent_task_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_subagent_tasks.id", ondelete="CASCADE"), unique=True
    )
    runtime_task_id: Mapped[str] = mapped_column(String(100))
    source_cursor: Mapped[str | None] = mapped_column(String(200))
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    usage_version: Mapped[int] = mapped_column(Integer, default=1)
    model_name: Mapped[str] = mapped_column(String(200))
    accumulated_cost_usd: Mapped[float] = mapped_column(Numeric(20, 8))
    prompt_tokens: Mapped[int] = mapped_column(BigInteger)
    completion_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger)
    context_window: Mapped[int] = mapped_column(BigInteger)
    per_turn_tokens: Mapped[int] = mapped_column(BigInteger)
    budget_limit_usd: Mapped[float | None] = mapped_column(Numeric(20, 8))
    budget_state: Mapped[str] = mapped_column(String(20), default="UNBOUNDED")
    budget_exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = (
    "AgentConversation",
    "AgentMessage",
    "MessageArtifactRef",
    "RuntimeCondensation",
    "RuntimeCondensationCommand",
    "RuntimeConversationFork",
    "RuntimeConfirmationBatch",
    "RuntimeSubagentTask",
    "RuntimeSubagentTaskUsage",
)
