from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class FlowRunConversationBinding(Base):
    """The only active FlowWeave model for an OpenHands Conversation."""

    __tablename__ = "flow_run_conversation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id",
            "openhands_conversation_id",
            name="uq_flow_run_conversation_runtime_identity",
        ),
        ForeignKeyConstraint(
            ["runtime_session_id", "flow_run_id"],
            ["flow_run_runtimes.id", "flow_run_runtimes.flow_run_id"],
            name="fk_flow_run_conversation_runtime_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(
        ForeignKey("flow_runs.id", ondelete="CASCADE"), index=True
    )
    runtime_session_id: Mapped[str] = mapped_column(String(36), index=True)
    openhands_conversation_id: Mapped[str] = mapped_column(String(100))
    display_label: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuntimeConfirmationApproval(Base):
    """Independent approval audit for one native pending-action batch.

    This row deliberately stores neither a Conversation state nor an event
    cursor. The pending action digest is an authorization boundary; OpenHands
    remains the source of the current pending batch and its event tree.
    """

    __tablename__ = "runtime_confirmation_approvals"
    __table_args__ = (
        Index(
            "uq_runtime_confirmation_approval_active",
            "flow_run_conversation_binding_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'DECIDING')"),
        ),
        CheckConstraint("action_count > 0", name="ck_runtime_confirmation_action_count"),
        CheckConstraint("state_version > 0", name="ck_runtime_confirmation_version"),
        CheckConstraint(
            "state IN ('PENDING', 'DECIDING', 'APPROVED', 'REJECTED', "
            "'EXPIRED', 'CANCELLED')",
            name="ck_runtime_confirmation_approval_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_conversation_binding_id: Mapped[str] = mapped_column(
        ForeignKey("flow_run_conversation_bindings.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="CASCADE"), index=True
    )
    pending_actions_digest: Mapped[str] = mapped_column(String(64))
    pending_actions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    risk_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    action_count: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    decision_accept: Mapped[bool | None] = mapped_column(Boolean)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(180), unique=True)
    decided_by: Mapped[str | None] = mapped_column(String(160))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ("FlowRunConversationBinding", "RuntimeConfirmationApproval")
