from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


class NodeDirectory(Base):
    __tablename__ = "node_directories"
    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_directory_parent_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("node_directories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    position: Mapped[int] = mapped_column(Integer, default=0)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class NodeAsset(Base):
    __tablename__ = "node_assets"
    __table_args__ = (UniqueConstraint("directory_id", "name", name="uq_asset_directory_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    directory_id: Mapped[str | None] = mapped_column(
        ForeignKey("node_directories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon_kind: Mapped[str] = mapped_column(String(30), default="LUCIDE")
    icon_value: Mapped[str] = mapped_column(String(80), default="bot")
    # The database foreign key is added by the 0014 forward migration. Keep
    # this mapping free of the cross-module FK so historical baseline
    # migrations that create catalog tables from current metadata do not try
    # to reference environment_versions before that table exists.
    environment_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class NodeIOField(Base):
    __tablename__ = "node_io_fields"
    __table_args__ = (
        UniqueConstraint(
            "node_asset_id", "direction", "field_key", name="uq_asset_direction_field"
        ),
        CheckConstraint("position >= 0", name="ck_io_position_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    node_asset_id: Mapped[str] = mapped_column(
        ForeignKey("node_assets.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(10))
    field_key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(160))
    data_type: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    template_url: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)


class NodeExecutorConfig(Base):
    __tablename__ = "node_executor_configs"
    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="ck_executor_timeout_positive"),
        CheckConstraint("max_iterations > 0", name="ck_executor_iterations_positive"),
    )

    node_asset_id: Mapped[str] = mapped_column(
        ForeignKey("node_assets.id", ondelete="CASCADE"), primary_key=True
    )
    model_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT")
    )
    model_name: Mapped[str | None] = mapped_column(String(200))
    startup_prompt: Mapped[str] = mapped_column(Text, default="")
    context_prompt: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=900)
    max_iterations: Mapped[int] = mapped_column(Integer, default=100)


class NodeCapabilityRef(Base):
    __tablename__ = "node_capability_refs"
    __table_args__ = (
        UniqueConstraint(
            "node_asset_id", "capability_type", "capability_key", name="uq_asset_capability"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    node_asset_id: Mapped[str] = mapped_column(
        ForeignKey("node_assets.id", ondelete="CASCADE"), index=True
    )
    capability_type: Mapped[str] = mapped_column(String(16))
    capability_key: Mapped[str] = mapped_column(String(200))
    normalized_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)


class CapabilityImport(Base):
    __tablename__ = "capability_imports"
    __table_args__ = (
        CheckConstraint(
            "state IN ('VALIDATED', 'COMMITTED', 'EXPIRED')",
            name="ck_capability_import_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    capability_type: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(20), default="VALIDATED", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SkillCollection(Base):
    __tablename__ = "skill_collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SkillCollectionItem(Base):
    __tablename__ = "skill_collection_items"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "capability_import_id",
            "capability_position",
            name="uq_skill_collection_capability_version",
        ),
        CheckConstraint("position >= 0", name="ck_skill_collection_item_position"),
        CheckConstraint("capability_position >= 0", name="ck_skill_collection_capability_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("skill_collections.id", ondelete="CASCADE"), index=True
    )
    capability_import_id: Mapped[str] = mapped_column(
        ForeignKey("capability_imports.id", ondelete="RESTRICT"), index=True
    )
    capability_position: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)


__all__ = (
    "NodeDirectory",
    "NodeAsset",
    "NodeIOField",
    "NodeExecutorConfig",
    "NodeCapabilityRef",
    "CapabilityImport",
    "SkillCollection",
    "SkillCollectionItem",
)
