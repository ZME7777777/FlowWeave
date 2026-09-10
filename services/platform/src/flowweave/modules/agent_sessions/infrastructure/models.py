"""Shared Agent-session locator and projection persistence mappings.

The table names deliberately remain stable while the default Agent Workspace
keeps compatibility imports.  A session binding belongs to the shared session
product; a product host only supplies its authorized host context and Runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class AgentConversationBinding(Base):
    __tablename__ = "agent_conversation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "runtime_session_id",
            "openhands_conversation_id",
            name="uq_agent_conversation_owner_runtime_id",
        ),
        CheckConstraint(
            "host_kind IN ('AGENT_WORKSPACE', 'FLOW_NODE')",
            name="ck_agent_conversation_host_kind",
        ),
        UniqueConstraint(
            "owner_user_id",
            "create_idempotency_key",
            name="uq_agent_conversation_owner_create_key",
        ),
        CheckConstraint(
            "working_directory IS NULL "
            "OR working_directory = '/runtime/workspace/project' "
            "OR working_directory LIKE '/runtime/workspace/project/%' "
            "OR working_directory ~ '^/runtime/workspace/"
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
            "(/.*)?$'",
            name="ck_agent_conversation_working_directory",
        ),
        CheckConstraint(
            "lifecycle IN ('PROVISIONING', 'ACTIVE', 'DELETE_PENDING', 'FAILED')",
            name="ck_agent_conversation_lifecycle",
        ),
        CheckConstraint(
            "title_state IN ('PENDING', 'GENERATED', 'MANUAL', 'FALLBACK')",
            name="ck_agent_conversation_title_state",
        ),
        CheckConstraint("title_generation >= 1", name="ck_agent_conversation_title_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # ``workspace_id`` remains the default-host compatibility reference.  A
    # FlowRun node uses the neutral host and lineage fields below instead.
    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # Runtime Session identity is host-neutral: Agent Workspace and FlowRun
    # Runtime Sessions live in separate ownership tables.
    runtime_session_id: Mapped[str] = mapped_column(String(36), index=True)
    host_kind: Mapped[str] = mapped_column(String(30), default="AGENT_WORKSPACE", index=True)
    host_id: Mapped[str] = mapped_column(String(36), index=True)
    conversation_scope_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    work_directory_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    working_directory: Mapped[str | None] = mapped_column(String(500))
    model_provider_id: Mapped[str | None] = mapped_column(String(36), index=True)
    model_name: Mapped[str | None] = mapped_column(String(240))
    reasoning_effort: Mapped[str | None] = mapped_column(String(30))
    streaming_callback_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    # OpenHands identifiers are UUIDs today, but the formal Runtime contract
    # permits opaque identifiers. Keep the shared locator compatible with the
    # existing FlowRun locator rather than silently truncating a future host.
    openhands_conversation_id: Mapped[str] = mapped_column(String(100))
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
    # A Worker-owned scheduling projection. It is intentionally separate from
    # the authoritative OpenHands usage counters and is only used to throttle
    # best-effort, read-only reconciliation.
    usage_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_reconcile_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class AgentConversationMessageAttachment(Base):
    """A display projection for files attached to a formal user MessageEvent."""

    __tablename__ = "agent_conversation_message_attachments"
    __table_args__ = (
        UniqueConstraint(
            "binding_id", "event_id", "path", name="uq_agent_conversation_message_attachment"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    binding_id: Mapped[str] = mapped_column(String(36), index=True)
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
            "capability_type IN ('SKILL', 'MCP', 'PLUGIN', 'CONTEXT')",
            name="ck_agent_conversation_capability_type",
        ),
        CheckConstraint("position >= 0", name="ck_agent_conversation_capability_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    binding_id: Mapped[str] = mapped_column(String(36), index=True)
    capability_version_id: Mapped[str] = mapped_column(String(36), index=True)
    capability_type: Mapped[str] = mapped_column(String(20))
    capability_key: Mapped[str] = mapped_column(String(160))
    digest: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentConversationCommand(Base):
    __tablename__ = "agent_conversation_commands"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_agent_conversation_command_owner_key",
        ),
        CheckConstraint(
            "command_type IN ('CREATE', 'DELETE', 'RENAME', 'FORK')", name="ck_agent_command_type"
        ),
        CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'AMBIGUOUS', 'FAILED')",
            name="ck_agent_command_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True)
    host_kind: Mapped[str] = mapped_column(String(30), default="AGENT_WORKSPACE", index=True)
    host_id: Mapped[str] = mapped_column(String(36), index=True)
    binding_id: Mapped[str] = mapped_column(String(36), index=True)
    command_type: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentConversationUsageBucket(Base):
    """Durable, monotonic projection of one OpenHands usage bucket.

    OpenHands keeps the authoritative absolute counters in Conversation state.
    ``baseline_*`` freezes the counters inherited when this binding was created
    (notably for forks); ``observed_*`` is the greatest valid source snapshot.
    Aggregates therefore use ``observed - baseline`` and never add a repeated
    event read twice.
    """

    __tablename__ = "agent_conversation_usage_buckets"
    __table_args__ = (
        UniqueConstraint("binding_id", "usage_id", name="uq_agent_conversation_usage_bucket"),
        CheckConstraint(
            "baseline_prompt_tokens >= 0 AND baseline_completion_tokens >= 0 "
            "AND baseline_cache_read_tokens >= 0 AND baseline_cache_write_tokens >= 0 "
            "AND baseline_reasoning_tokens >= 0 AND baseline_cost_usd >= 0 "
            "AND observed_prompt_tokens >= baseline_prompt_tokens "
            "AND observed_completion_tokens >= baseline_completion_tokens "
            "AND observed_cache_read_tokens >= baseline_cache_read_tokens "
            "AND observed_cache_write_tokens >= baseline_cache_write_tokens "
            "AND observed_reasoning_tokens >= baseline_reasoning_tokens "
            "AND observed_cost_usd >= baseline_cost_usd",
            name="ck_agent_conversation_usage_monotonic",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # The locator can be deleted after the cost has been attributed; do not use
    # a database FK/cascade here or historical Attempt/NodeRun totals vanish.
    binding_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    openhands_conversation_id: Mapped[str] = mapped_column(String(100), index=True)
    usage_id: Mapped[str] = mapped_column(String(200))
    usage_kind: Mapped[str] = mapped_column(String(20), default="AUXILIARY")
    model_name: Mapped[str] = mapped_column(String(200), default="default")
    baseline_cost_usd: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    observed_cost_usd: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    baseline_prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    baseline_completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    baseline_cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    baseline_cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    baseline_reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


def _normalize_default_host(
    _mapper: object,
    _connection: object,
    target: AgentConversationBinding | AgentConversationCommand,
) -> None:
    """Retain the historical Workspace-only construction contract.

    Direct callers predating the multi-host locator supplied only
    ``workspace_id``. FlowRun callers always provide explicit host lineage.
    """

    workspace_id = cast(str | None, getattr(target, "workspace_id", None))
    host_kind = cast(str | None, getattr(target, "host_kind", None))
    if host_kind not in {None, "AGENT_WORKSPACE"} or not workspace_id:
        return
    target.host_kind = "AGENT_WORKSPACE"
    if not cast(str | None, getattr(target, "host_id", None)):
        target.host_id = workspace_id
    if isinstance(target, AgentConversationBinding) and not cast(
        str | None, getattr(target, "conversation_scope_id", None)
    ):
        target.conversation_scope_id = workspace_id


event.listen(AgentConversationBinding, "before_insert", _normalize_default_host)
event.listen(AgentConversationCommand, "before_insert", _normalize_default_host)


__all__ = (
    "AgentConversationBinding",
    "AgentConversationCapability",
    "AgentConversationCommand",
    "AgentConversationMessageAttachment",
)
