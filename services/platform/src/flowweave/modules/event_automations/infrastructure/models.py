from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class EventTriggerVersion(Base):
    """Immutable version of one user-owned event trigger definition."""

    __tablename__ = "event_trigger_versions"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "trigger_key",
            "version_no",
            name="uq_event_trigger_version_owner_key_no",
        ),
        CheckConstraint("version_no >= 1", name="ck_event_trigger_version_positive"),
        CheckConstraint("length(trim(trigger_key)) > 0", name="ck_event_trigger_key_nonblank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    trigger_key: Mapped[str] = mapped_column(String(120), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    event_filter_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventTriggerAction(Base):
    """Ordered action belonging to a frozen trigger version."""

    __tablename__ = "event_trigger_actions"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "trigger_version_id",
            "position",
            name="uq_event_trigger_action_owner_version_position",
        ),
        CheckConstraint("position >= 0", name="ck_event_trigger_action_position_nonnegative"),
        CheckConstraint(
            "action_type IN ('WEBHOOK', 'NOTIFY', 'CREATE_TASK')",
            name="ck_event_trigger_action_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    trigger_version_id: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(60))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventTriggerDelivery(Base):
    """Durable, at-least-once delivery intent for one trigger action."""

    __tablename__ = "event_trigger_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_event_trigger_delivery_owner_key"
        ),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'RETRY', 'DEAD')",
            name="ck_event_trigger_delivery_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_event_trigger_delivery_attempts_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    trigger_version_id: Mapped[str] = mapped_column(String(36), index=True)
    trigger_action_id: Mapped[str] = mapped_column(String(36), index=True)
    event_id: Mapped[str] = mapped_column(String(200), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ("EventTriggerAction", "EventTriggerDelivery", "EventTriggerVersion")
