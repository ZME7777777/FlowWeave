from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
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
    __table_args__ = (
        UniqueConstraint(
            "directory_id",
            "name",
            name="uq_asset_directory_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    directory_id: Mapped[str | None] = mapped_column(
        ForeignKey("node_directories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon_kind: Mapped[str] = mapped_column(String(30), default="LUCIDE")
    icon_value: Mapped[str] = mapped_column(String(80), default="bot")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
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
    position: Mapped[int] = mapped_column(Integer, default=0)


class NodeExecutorConfig(Base):
    __tablename__ = "node_executor_configs"

    node_asset_id: Mapped[str] = mapped_column(
        ForeignKey("node_assets.id", ondelete="CASCADE"), primary_key=True
    )
    startup_prompt: Mapped[str] = mapped_column(Text, default="")
    context_prompt: Mapped[str] = mapped_column(Text, default="")


class NodeContextCapability(Base):
    """An ordered, immutable Context Version selected by a node asset."""

    __tablename__ = "node_context_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "node_asset_id", "capability_version_id", name="uq_node_context_capability"
        ),
        CheckConstraint("position >= 0", name="ck_node_context_capability_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    node_asset_id: Mapped[str] = mapped_column(
        ForeignKey("node_assets.id", ondelete="CASCADE"), index=True
    )
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)


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


class CapabilityBlob(Base):
    """Content-addressed immutable bytes referenced by published versions."""

    __tablename__ = "capability_blobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CapabilityPackage(Base):
    """Stable capability identity; mutable labels never enter Runtime snapshots."""

    __tablename__ = "capability_packages"
    __table_args__ = (
        UniqueConstraint(
            "capability_type", "capability_key", name="uq_capability_package_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    capability_type: Mapped[str] = mapped_column(String(32), index=True)
    capability_key: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CapabilityVersion(Base):
    """Published immutable Runtime configuration and source digest."""

    __tablename__ = "capability_versions"
    __table_args__ = (
        UniqueConstraint("package_id", "version_no", name="uq_capability_version_number"),
        UniqueConstraint(
            "source_import_id",
            "source_position",
            name="uq_capability_version_import_position",
        ),
        CheckConstraint("version_no > 0", name="ck_capability_version_number_positive"),
        CheckConstraint("state IN ('PUBLISHED', 'RETIRED')", name="ck_capability_version_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("capability_packages.id", ondelete="RESTRICT"), index=True
    )
    blob_id: Mapped[str] = mapped_column(
        ForeignKey("capability_blobs.id", ondelete="RESTRICT"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    normalized_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_filename: Mapped[str] = mapped_column(String(255))
    source_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_imports.id", ondelete="SET NULL"), index=True
    )
    source_position: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="PUBLISHED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CapabilityDependency(Base):
    __tablename__ = "capability_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "capability_version_id",
            "ecosystem",
            "name",
            name="uq_capability_dependency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="CASCADE"), index=True
    )
    ecosystem: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(100))


class CapabilityValidation(Base):
    __tablename__ = "capability_validations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'PASSED', 'FAILED')",
            name="ck_capability_validation_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="CASCADE"), index=True
    )
    validator: Mapped[str] = mapped_column(String(100), default="flowweave-import-v1")
    status: Mapped[str] = mapped_column(String(20))
    # Migration 0038 installs the cross-module FK. Keep current metadata free
    # of it because historical baseline migrations create catalog tables before
    # environment tables exist.
    environment_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MCPOAuthSecretReference(Base):
    """Governed encrypted OpenHands OAuth state for one MCP target environment."""

    __tablename__ = "mcp_oauth_secret_references"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ACTIVE', 'REVOKED')",
            name="ck_mcp_oauth_secret_reference_state",
        ),
        CheckConstraint(
            "state_version > 0",
            name="ck_mcp_oauth_secret_reference_version_positive",
        ),
        UniqueConstraint(
            "capability_version_id",
            "environment_version_id",
            name="uq_mcp_oauth_secret_reference_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    # Migration 0039 installs this cross-module FK; historical baseline
    # migrations create catalog metadata before environment tables exist.
    environment_version_id: Mapped[str] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_oauth_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_state_digest: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class MCPOAuthSecretAudit(Base):
    """Redacted lifecycle fact; OAuth values never enter the audit payload."""

    __tablename__ = "mcp_oauth_secret_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('CREATED', 'AUTHORIZED', 'REFRESHED', 'REVOKED')",
            name="ck_mcp_oauth_secret_audit_action",
        ),
        CheckConstraint(
            "state_version > 0",
            name="ck_mcp_oauth_secret_audit_version_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    secret_reference_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_oauth_secret_references.id", ondelete="RESTRICT"), index=True
    )
    validation_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_validations.id", ondelete="SET NULL"), index=True
    )
    authorization_id: Mapped[str | None] = mapped_column(
        ForeignKey("mcp_oauth_authorizations.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(20))
    state_version: Mapped[int] = mapped_column(Integer)
    oauth_state_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MCPOAuthAuthorization(Base):
    """Durable binding to one formal OpenHands in-Runtime OAuth job."""

    __tablename__ = "mcp_oauth_authorizations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'AUTHORIZING', 'SUCCEEDED', 'FAILED', 'EXPIRED')",
            name="ck_mcp_oauth_authorization_state",
        ),
        CheckConstraint(
            "state_version > 0",
            name="ck_mcp_oauth_authorization_version_positive",
        ),
        CheckConstraint(
            "expected_secret_version > 0",
            name="ck_mcp_oauth_authorization_secret_version_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    secret_reference_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_oauth_secret_references.id", ondelete="RESTRICT"), index=True
    )
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    # Migration 0040 owns cross-module FKs to Environment and Sandbox.
    environment_version_id: Mapped[str] = mapped_column(String(36), index=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    expected_secret_version: Mapped[int] = mapped_column(Integer)
    persisted_secret_version: Mapped[int | None] = mapped_column(Integer)
    runtime_job_id: Mapped[str | None] = mapped_column(String(100))
    runtime_resource_name: Mapped[str | None] = mapped_column(String(100))
    runtime_base_url: Mapped[str | None] = mapped_column(Text)
    encrypted_authorization_url: Mapped[bytes | None] = mapped_column(LargeBinary)
    callback_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_catalog_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PluginSourceResolution(Base):
    """Durable publication workflow for one immutable remote Plugin source."""

    __tablename__ = "plugin_source_resolutions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'READY', 'PUBLISHED', 'FAILED', 'EXPIRED')",
            name="ck_plugin_source_resolution_state",
        ),
        CheckConstraint(
            "source_kind IN ('GIT', 'MARKETPLACE')",
            name="ck_plugin_source_resolution_kind",
        ),
        CheckConstraint("state_version > 0", name="ck_plugin_source_resolution_version_positive"),
        UniqueConstraint(
            "source_kind",
            "source_url",
            "requested_commit",
            "repo_path",
            "marketplace_plugin_name",
            name="uq_plugin_source_resolution_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_kind: Mapped[str] = mapped_column(String(20), default="GIT")
    source_url: Mapped[str] = mapped_column(Text)
    requested_commit: Mapped[str] = mapped_column(String(40))
    repo_path: Mapped[str] = mapped_column(Text, default="")
    marketplace_plugin_name: Mapped[str] = mapped_column(String(128), default="")
    resolved_source_url: Mapped[str | None] = mapped_column(Text)
    resolved_commit: Mapped[str | None] = mapped_column(String(40))
    resolved_repo_path: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resolver_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_detail: Mapped[str | None] = mapped_column(Text)
    capability_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class MemorySource(Base):
    """Stable governed identity for an append-only Memory content lineage."""

    __tablename__ = "memory_sources"
    __table_args__ = (
        CheckConstraint("scope IN ('USER', 'PROJECT')", name="ck_memory_source_scope"),
        UniqueConstraint(
            "scope", "scope_key", "source_key", name="uq_memory_source_scope_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_key: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    scope: Mapped[str] = mapped_column(String(20), index=True)
    scope_key: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemorySourceVersion(Base):
    """Immutable Memory bytes plus mutable governance decisions."""

    __tablename__ = "memory_source_versions"
    __table_args__ = (
        CheckConstraint("version_no > 0", name="ck_memory_source_version_positive"),
        CheckConstraint("byte_size > 0", name="ck_memory_source_version_size_positive"),
        CheckConstraint(
            "review_status IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_memory_source_version_review",
        ),
        CheckConstraint(
            "sensitive_data_status IN ('NOT_SCANNED', 'PASSED', 'BLOCKED')",
            name="ck_memory_source_version_sensitive_data",
        ),
        CheckConstraint(
            "lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED', 'EXPIRED')",
            name="ck_memory_source_version_lifecycle",
        ),
        CheckConstraint(
            "governance_version > 0", name="ck_memory_source_version_governance_positive"
        ),
        CheckConstraint(
            "lifecycle_state != 'ACTIVE' OR "
            "(review_status = 'APPROVED' AND sensitive_data_status = 'PASSED')",
            name="ck_memory_source_version_active_governed",
        ),
        UniqueConstraint("source_id", "version_no", name="uq_memory_source_version_number"),
        UniqueConstraint("source_id", "digest", name="uq_memory_source_version_digest"),
        UniqueConstraint("previous_version_id", name="uq_memory_source_previous_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("memory_sources.id", ondelete="RESTRICT"), index=True
    )
    previous_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_source_versions.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    sensitive_data_status: Mapped[str] = mapped_column(
        String(20), default="NOT_SCANNED", index=True
    )
    lifecycle_state: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    governance_version: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    sensitive_data_scanner: Mapped[str | None] = mapped_column(String(100))
    sensitive_data_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sensitive_data_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_days: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemorySourceVersionReference(Base):
    """Immutable retention hold from a frozen policy or Run Snapshot."""

    __tablename__ = "memory_source_version_references"
    __table_args__ = (
        CheckConstraint(
            "reference_kind IN ('POLICY_VERSION', 'RUN_SNAPSHOT')",
            name="ck_memory_source_version_reference_kind",
        ),
        UniqueConstraint(
            "memory_source_version_id",
            "reference_kind",
            "reference_id",
            name="uq_memory_source_version_reference",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    memory_source_version_id: Mapped[str] = mapped_column(
        ForeignKey("memory_source_versions.id", ondelete="RESTRICT"), index=True
    )
    reference_kind: Mapped[str] = mapped_column(String(30), index=True)
    reference_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CapabilityCollection(Base):
    __tablename__ = "capability_collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CapabilityCollectionItem(Base):
    __tablename__ = "capability_collection_items"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "capability_version_id",
            name="uq_capability_collection_version",
        ),
        CheckConstraint("position >= 0", name="ck_capability_collection_item_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("capability_collections.id", ondelete="CASCADE"), index=True
    )
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)


__all__ = (
    "NodeDirectory",
    "NodeAsset",
    "NodeIOField",
    "NodeExecutorConfig",
    "NodeContextCapability",
    "CapabilityImport",
    "CapabilityBlob",
    "CapabilityPackage",
    "CapabilityVersion",
    "CapabilityDependency",
    "CapabilityValidation",
    "MCPOAuthAuthorization",
    "MCPOAuthSecretReference",
    "MCPOAuthSecretAudit",
    "PluginSourceResolution",
    "MemorySource",
    "MemorySourceVersion",
    "CapabilityCollection",
    "CapabilityCollectionItem",
)
