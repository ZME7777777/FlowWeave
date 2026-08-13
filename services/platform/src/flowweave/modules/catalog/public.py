"""Stable public facade for the catalog module."""

from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_imports import (
    cleanup_expired_import,
    process_dependency_build,
)
from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.modules.catalog.application.mcp_oauth_authorizations import (
    authorization_owner_is_active,
)
from flowweave.modules.catalog.application.memory_sources import (
    GovernedMemoryMaterial,
    register_snapshot_references,
    resolve_snapshot_material,
)
from flowweave.modules.catalog.application.plugin_sources import (
    expire_resolution,
    process_resolution,
    record_resolution_failure,
)
from flowweave.modules.catalog.application.service import asset_dict
from flowweave.shared.models import CapabilityValidation, NodeAsset


def cleanup_capability_import(db: Session, import_id: str) -> None:
    """Expire an import session and reclaim its unreferenced source object."""

    cleanup_expired_import(db, import_id)


def build_capability_dependencies(db: Session, import_id: str, position: int) -> None:
    """Build one immutable, pinned dependency bundle in the configured builder."""

    process_dependency_build(db, import_id, position)


def resolve_plugin_source(db: Session, resolution_id: str) -> None:
    """Resolve a pinned remote Plugin into a canonical local ZIP."""

    process_resolution(db, resolution_id)


def expire_plugin_source(db: Session, resolution_id: str) -> None:
    """Expire an unpublished remote Plugin resolution."""

    expire_resolution(db, resolution_id)


def fail_plugin_source_resolution(db: Session, resolution_id: str, error: str) -> None:
    """Project a terminal resolver failure for audit and retry."""

    record_resolution_failure(db, resolution_id, error)


def describe_asset(db: Session, asset: NodeAsset) -> dict[str, object]:
    """Return the stable read model used by flow snapshots."""

    return asset_dict(db, asset)


def describe_agent_profile_version(
    db: Session, version_id: str, *, include_retired: bool = False
) -> dict[str, object]:
    """Return frozen Profile identity/config for orchestration commands."""

    published = resolve_version(db, version_id, include_retired=include_retired)
    if published.package.capability_type != "AGENT_PROFILE":
        raise ValueError("Capability is not an Agent Profile")
    return {
        "capability_version_id": published.version.id,
        "capability_type": published.package.capability_type,
        "capability_key": published.package.capability_key,
        "digest": published.version.digest,
        "content_hash": published.blob.content_hash,
        "runtime_config": published.runtime_config(),
        "state": published.version.state,
    }


def capability_validation_owner_is_active(db: Session, validation_id: str) -> bool:
    """Authorize a temporary Runtime only while its durable validation is running."""

    item = db.get(CapabilityValidation, validation_id)
    return item is not None and item.status == "RUNNING"


def mcp_oauth_authorization_owner_is_active(db: Session, authorization_id: str) -> bool:
    """Authorize a temporary Runtime only while its OAuth job is active."""

    return authorization_owner_is_active(db, authorization_id)


def hold_snapshot_memory_references(
    db: Session, *, snapshot_id: str, runtime_manifest: dict[str, object]
) -> None:
    """Freeze retention holds for Memory versions referenced by a Snapshot."""

    register_snapshot_references(db, snapshot_id=snapshot_id, runtime_manifest=runtime_manifest)


def resolve_snapshot_memory(
    db: Session,
    *,
    snapshot_id: str,
    source_refs: list[dict[str, str]],
    allowed_scopes: set[str],
) -> tuple[GovernedMemoryMaterial, ...]:
    """Return ephemeral governed Memory bytes for Runtime materialization."""

    return resolve_snapshot_material(
        db,
        snapshot_id=snapshot_id,
        source_refs=source_refs,
        allowed_scopes=allowed_scopes,
    )


__all__ = (
    "build_capability_dependencies",
    "capability_validation_owner_is_active",
    "cleanup_capability_import",
    "describe_asset",
    "describe_agent_profile_version",
    "expire_plugin_source",
    "fail_plugin_source_resolution",
    "hold_snapshot_memory_references",
    "mcp_oauth_authorization_owner_is_active",
    "resolve_plugin_source",
    "resolve_snapshot_memory",
)
