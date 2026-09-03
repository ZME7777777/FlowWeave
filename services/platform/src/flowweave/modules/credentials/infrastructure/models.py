"""Encrypted, host-scoped credentials for Agent web access."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class WebsiteCredential(Base):
    __tablename__ = "website_credentials"
    __table_args__ = (
        UniqueConstraint("target_host", "name", name="uq_website_credential_host_name"),
        CheckConstraint(
            "auth_type IN ('USERNAME_PASSWORD', 'BEARER_TOKEN')",
            name="ck_website_credential_auth_type",
        ),
        CheckConstraint("row_version >= 1", name="ck_website_credential_row_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    target_host: Mapped[str] = mapped_column(String(253), index=True)
    include_subdomains: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_type: Mapped[str] = mapped_column(String(30))
    encrypted_username: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary)
    secret_hint: Mapped[str | None] = mapped_column(String(20))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
