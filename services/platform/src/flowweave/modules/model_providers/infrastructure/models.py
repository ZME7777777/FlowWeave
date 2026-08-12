from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(String(30), default="API_KEY")
    encrypted_api_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    api_key_hint: Mapped[str | None] = mapped_column(String(20))
    encrypted_oauth_access_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_oauth_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_account_id: Mapped[str | None] = mapped_column(String(240))
    oauth_email: Mapped[str | None] = mapped_column(String(320))
    encrypted_oauth_device_auth_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_user_code: Mapped[str | None] = mapped_column(String(80))
    oauth_device_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_poll_interval: Mapped[int | None] = mapped_column(Integer)
    connection_state: Mapped[str] = mapped_column(String(30), default="UNTESTED")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_name", name="uq_provider_model_name"),
        Index(
            "uq_provider_default_model",
            "provider_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("model_providers.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(240))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    default_reasoning_effort: Mapped[str | None] = mapped_column(String(30))
    supported_reasoning_efforts: Mapped[list[str]] = mapped_column(JSON, default=list)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = (
    "ModelProvider",
    "ProviderModel",
)
