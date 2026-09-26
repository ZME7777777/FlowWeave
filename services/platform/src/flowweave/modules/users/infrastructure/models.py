from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('SUPER_ADMIN', 'USER')", name="ck_user_role"),)
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class UserSession(Base):
    __tablename__ = "user_sessions"
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserOperationLog(Base):
    __tablename__ = "user_operation_logs"
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    method: Mapped[str] = mapped_column(String(10))
    route: Mapped[str] = mapped_column(String(500), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    client_ip: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AdminRuntimeOperation(Base):
    """Append-only audit fact for one administrator Runtime replacement request."""

    __tablename__ = "admin_runtime_operations"
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None  # pyright: ignore[reportIncompatibleVariableOverride]
    __table_args__ = (
        CheckConstraint("action = 'REPLACE_RUNTIME'", name="ck_admin_runtime_operation_action"),
        CheckConstraint(
            "runtime_kind IN ('FLOW_RUN', 'AGENT_WORKSPACE')",
            name="ck_admin_runtime_operation_runtime_kind",
        ),
        CheckConstraint("expected_generation >= 1", name="ck_admin_runtime_operation_generation"),
        CheckConstraint(
            "expected_session_row_version >= 1", name="ck_admin_runtime_operation_version"
        ),
        CheckConstraint("status IN ('SUBMITTED')", name="ck_admin_runtime_operation_status"),
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_admin_runtime_operation_actor_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_user_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_username: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40), default="REPLACE_RUNTIME")
    runtime_kind: Mapped[str] = mapped_column(String(30), default="FLOW_RUN", index=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    runtime_session_id: Mapped[str] = mapped_column(String(36), index=True)
    expected_generation: Mapped[int] = mapped_column(Integer)
    expected_session_row_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AdminAlertState(Base):
    __tablename__ = "admin_alert_states"
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None  # pyright: ignore[reportIncompatibleVariableOverride]

    alert_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[str | None] = mapped_column(String(36))
    acknowledged_by_username: Mapped[str | None] = mapped_column(String(80))
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AdminAlertAction(Base):
    __tablename__ = "admin_alert_actions"
    __tenant_scoped__ = False
    owner_user_id: ClassVar[None] = None  # pyright: ignore[reportIncompatibleVariableOverride]
    __table_args__ = (
        CheckConstraint("action IN ('ACKNOWLEDGE', 'SILENCE')", name="ck_admin_alert_action"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    alert_key: Mapped[str] = mapped_column(String(300), index=True)
    action: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_username: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(500))
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


__all__ = (
    "AdminAlertAction",
    "AdminAlertState",
    "AdminRuntimeOperation",
    "User",
    "UserOperationLog",
    "UserSession",
)
