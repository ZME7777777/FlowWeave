from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class TerminalEnvironment(Base):
    __tablename__ = "terminal_environments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    base_image: Mapped[str] = mapped_column(String(500))
    # The user-facing repository@manifest-digest reference and the resolved
    # local content digest are stored separately. Runtime creation only uses
    # the latter, so a registry tag can never drift underneath a version.
    base_image_digest: Mapped[str | None] = mapped_column(String(100))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    # High-water mark retained when historical versions are deleted so a
    # published version number is never reused.
    last_version_no: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class EnvironmentVersion(Base):
    __tablename__ = "environment_versions"
    __table_args__ = (
        UniqueConstraint("environment_id", "version_no", name="uq_environment_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    environment_id: Mapped[str] = mapped_column(String(36), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(30), default="PUBLISHING", index=True)
    base_image_reference: Mapped[str] = mapped_column(String(500), default="")
    base_image_digest: Mapped[str] = mapped_column(String(100), default="")
    image_reference: Mapped[str] = mapped_column(String(500), default="")
    image_digest: Mapped[str] = mapped_column(String(100), default="")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EnvironmentSetupSession(Base):
    __tablename__ = "environment_setup_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    environment_id: Mapped[str] = mapped_column(String(36), index=True)
    base_version_id: Mapped[str | None] = mapped_column(String(36))
    sandbox_id: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    published_version_id: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(30), default="STARTING", index=True)
    container_id: Mapped[str] = mapped_column(String(100), default="")
    base_image_reference: Mapped[str] = mapped_column(String(500))
    base_image_digest: Mapped[str] = mapped_column(String(100), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ("EnvironmentSetupSession", "EnvironmentVersion", "TerminalEnvironment")
