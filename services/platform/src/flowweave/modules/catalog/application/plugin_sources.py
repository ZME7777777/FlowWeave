from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_imports import (
    validate_plugin_archive,
)
from flowweave.modules.catalog.application.capability_repository import (
    publish_import,
    resolve_version,
)
from flowweave.modules.tasks.public import enqueue
from flowweave.shared.application.plugin_resolver import (
    MarketplaceCatalogRequest,
    MarketplacePluginResolveRequest,
    PluginResolveRequest,
)
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.plugin_resolver import (
    configured_plugin_hosts,
    validate_plugin_git_source,
)
from flowweave.shared.models import (
    CapabilityBlob,
    CapabilityImport,
    CapabilityValidation,
    CapabilityVersion,
    PluginSourceResolution,
)
from flowweave.shared.plugin_resolver import get_plugin_resolver
from flowweave.shared.schemas import (
    MarketplaceCatalogWrite,
    MarketplacePluginSourceResolveWrite,
    PluginSourceResolveWrite,
)
from flowweave.shared.settings import get_settings


@dataclass(frozen=True, slots=True)
class PluginSourcePublishPlan:
    resolution_id: str
    expected_state_version: int
    storage_key: str
    content_hash: str
    byte_size: int
    preview: dict[str, Any]


def list_marketplace_catalog(payload: MarketplaceCatalogWrite) -> dict[str, object]:
    marketplace = validate_plugin_git_source(
        PluginResolveRequest(
            payload.marketplace_source_url,
            payload.marketplace_commit,
            payload.marketplace_repo_path,
        ),
        configured_plugin_hosts(get_settings()),
    )
    return get_plugin_resolver().list_marketplace(
        MarketplaceCatalogRequest(marketplace.source, marketplace.commit, marketplace.repo_path)
    )


def _read_model(item: PluginSourceResolution) -> dict[str, Any]:
    capability = None
    if item.capability_version_id:
        capability = {
            "capability_id": item.capability_version_id,
            "capability_type": "PLUGIN",
        }
    return {
        "id": item.id,
        "source_kind": item.source_kind,
        "source_url": item.source_url,
        "requested_commit": item.requested_commit,
        "repo_path": item.repo_path or None,
        "marketplace_plugin_name": item.marketplace_plugin_name or None,
        "resolved_source_url": item.resolved_source_url,
        "resolved_commit": item.resolved_commit,
        "resolved_repo_path": item.resolved_repo_path,
        "state": item.state,
        "state_version": item.state_version,
        "content_hash": item.content_hash,
        "byte_size": item.byte_size,
        "preview": item.preview_json,
        "resolver_report": item.resolver_report_json,
        "error_detail": item.error_detail,
        "capability": capability,
        "expires_at": item.expires_at.isoformat(),
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def create_resolution(db: Session, payload: PluginSourceResolveWrite) -> dict[str, Any]:
    request = validate_plugin_git_source(
        PluginResolveRequest(payload.source_url, payload.commit, payload.repo_path),
        configured_plugin_hosts(get_settings()),
    )
    repo_path = request.repo_path or ""
    existing = db.scalar(
        select(PluginSourceResolution).where(
            PluginSourceResolution.source_kind == "GIT",
            PluginSourceResolution.source_url == request.source,
            PluginSourceResolution.requested_commit == request.commit,
            PluginSourceResolution.repo_path == repo_path,
            PluginSourceResolution.marketplace_plugin_name == "",
        )
    )
    return _create_resolution(
        db,
        source_kind="GIT",
        source_url=request.source,
        requested_commit=request.commit,
        repo_path=repo_path,
        marketplace_plugin_name="",
        existing=existing,
    )


def create_marketplace_resolution(
    db: Session, payload: MarketplacePluginSourceResolveWrite
) -> dict[str, Any]:
    marketplace = validate_plugin_git_source(
        PluginResolveRequest(
            payload.marketplace_source_url,
            payload.marketplace_commit,
            payload.marketplace_repo_path,
        ),
        configured_plugin_hosts(get_settings()),
    )
    repo_path = marketplace.repo_path or ""
    existing = db.scalar(
        select(PluginSourceResolution).where(
            PluginSourceResolution.source_kind == "MARKETPLACE",
            PluginSourceResolution.source_url == marketplace.source,
            PluginSourceResolution.requested_commit == marketplace.commit,
            PluginSourceResolution.repo_path == repo_path,
            PluginSourceResolution.marketplace_plugin_name == payload.plugin_name,
        )
    )
    return _create_resolution(
        db,
        source_kind="MARKETPLACE",
        source_url=marketplace.source,
        requested_commit=marketplace.commit,
        repo_path=repo_path,
        marketplace_plugin_name=payload.plugin_name,
        existing=existing,
    )


def _create_resolution(
    db: Session,
    *,
    source_kind: str,
    source_url: str,
    requested_commit: str,
    repo_path: str,
    marketplace_plugin_name: str,
    existing: PluginSourceResolution | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    if existing is not None:
        if existing.state in {"FAILED", "EXPIRED"}:
            existing.state = "PENDING"
            existing.state_version += 1
            existing.error_detail = None
            existing.content_hash = None
            existing.storage_key = None
            existing.byte_size = None
            existing.preview_json = {}
            existing.resolver_report_json = {}
            existing.resolved_source_url = None
            existing.resolved_commit = None
            existing.resolved_repo_path = None
            existing.resolved_at = None
            existing.expires_at = now + timedelta(
                seconds=get_settings().capability_import_ttl_seconds
            )
            enqueue(
                db,
                task_type="RESOLVE_PLUGIN_SOURCE",
                aggregate_type="PLUGIN_SOURCE_RESOLUTION",
                aggregate_id=existing.id,
                idempotency_key=(f"resolve-plugin-source:{existing.id}:{existing.state_version}"),
            )
            enqueue(
                db,
                task_type="EXPIRE_PLUGIN_SOURCE",
                aggregate_type="PLUGIN_SOURCE_RESOLUTION",
                aggregate_id=existing.id,
                idempotency_key=(f"expire-plugin-source:{existing.id}:{existing.state_version}"),
                available_at=existing.expires_at,
            )
        finish(db)
        return _read_model(existing)

    item = PluginSourceResolution(
        source_kind=source_kind,
        source_url=source_url,
        requested_commit=requested_commit,
        repo_path=repo_path,
        marketplace_plugin_name=marketplace_plugin_name,
        expires_at=now + timedelta(seconds=get_settings().capability_import_ttl_seconds),
    )
    db.add(item)
    db.flush()
    enqueue(
        db,
        task_type="RESOLVE_PLUGIN_SOURCE",
        aggregate_type="PLUGIN_SOURCE_RESOLUTION",
        aggregate_id=item.id,
        idempotency_key=f"resolve-plugin-source:{item.id}:1",
    )
    enqueue(
        db,
        task_type="EXPIRE_PLUGIN_SOURCE",
        aggregate_type="PLUGIN_SOURCE_RESOLUTION",
        aggregate_id=item.id,
        idempotency_key=f"expire-plugin-source:{item.id}:1",
        available_at=item.expires_at,
    )
    finish(db)
    return _read_model(item)


def read_resolution(db: Session, resolution_id: str) -> dict[str, Any]:
    item = db.get(PluginSourceResolution, resolution_id)
    if item is None:
        raise DomainError("PLUGIN_SOURCE_NOT_FOUND", "Plugin source resolution was not found", 404)
    return _read_model(item)


def process_resolution(db: Session, resolution_id: str) -> None:
    item = db.get(PluginSourceResolution, resolution_id)
    if item is None or item.state != "PENDING":
        return
    expected_version = item.state_version
    source = item.source_url
    commit = item.requested_commit
    repo_path = item.repo_path or None
    source_kind = item.source_kind
    marketplace_plugin_name = item.marketplace_plugin_name
    expires_at = item.expires_at
    now = datetime.now(UTC)
    if expires_at <= now:
        db.execute(
            update(PluginSourceResolution)
            .where(
                PluginSourceResolution.id == resolution_id,
                PluginSourceResolution.state == "PENDING",
                PluginSourceResolution.state_version == expected_version,
                PluginSourceResolution.expires_at <= now,
            )
            .values(
                state="EXPIRED",
                state_version=expected_version + 1,
                updated_at=now,
            )
        )
        finish(db)
        return

    # Git access occurs only after releasing every application DB lock.
    db.rollback()
    if source_kind == "MARKETPLACE":
        bundle = get_plugin_resolver().resolve_marketplace_plugin(
            MarketplacePluginResolveRequest(
                marketplace_source=source,
                marketplace_commit=commit,
                marketplace_repo_path=repo_path,
                plugin_name=marketplace_plugin_name,
            )
        )
        if not bundle.resolved_source:
            raise RuntimeError("Marketplace resolver omitted the immutable Plugin source")
        resolved = validate_plugin_git_source(
            PluginResolveRequest(
                bundle.resolved_source,
                bundle.resolved_commit,
                bundle.resolved_repo_path,
            ),
            configured_plugin_hosts(get_settings()),
        )
        archive_name = marketplace_plugin_name
    else:
        bundle = get_plugin_resolver().resolve(PluginResolveRequest(source, commit, repo_path))
        resolved = validate_plugin_git_source(
            PluginResolveRequest(
                bundle.resolved_source or source,
                bundle.resolved_commit,
                bundle.resolved_repo_path if bundle.resolved_source else repo_path,
            ),
            configured_plugin_hosts(get_settings()),
        )
        if resolved.commit != commit:
            raise RuntimeError("Plugin resolver changed the requested commit")
        archive_name = Path(repo_path or source.rstrip("/")).stem
    preview = validate_plugin_archive(bundle.content, archive_name)
    raw_capabilities: object = preview.get("capabilities")
    capabilities = (
        cast(list[object], raw_capabilities) if isinstance(raw_capabilities, list) else []
    )
    if len(capabilities) != 1 or not isinstance(capabilities[0], dict):
        raise RuntimeError("Resolved Plugin did not produce one immutable capability")
    normalized = cast(dict[str, Any], capabilities[0]).get("normalized_config")
    file_hashes = (
        cast(dict[str, Any], normalized).get("file_hashes")
        if isinstance(normalized, dict)
        else None
    )
    if bundle.report.get("file_hashes") != file_hashes:
        raise RuntimeError("Plugin resolver report does not match validated bytes")
    digest = hashlib.sha256(bundle.content).hexdigest()
    # Each workflow owns a distinct staging object. Equal Plugin bytes from
    # concurrent Git sources therefore cannot delete one another while a CAS
    # loses or a READY workflow expires. On publication this exact key becomes
    # the immutable Blob key, unless an existing content-addressed Blob wins.
    storage_key = f"plugin-sources/{digest[:2]}/{digest}-{resolution_id}-{uuid4().hex}.zip"
    get_artifact_store().put(storage_key, bundle.content)

    now = datetime.now(UTC)
    if expires_at <= now:
        get_artifact_store().delete(storage_key)
        db.execute(
            update(PluginSourceResolution)
            .where(
                PluginSourceResolution.id == resolution_id,
                PluginSourceResolution.state == "PENDING",
                PluginSourceResolution.state_version == expected_version,
            )
            .values(state="EXPIRED", state_version=expected_version + 1)
        )
        finish(db)
        return
    claimed = db.scalar(
        update(PluginSourceResolution)
        .where(
            PluginSourceResolution.id == resolution_id,
            PluginSourceResolution.state == "PENDING",
            PluginSourceResolution.state_version == expected_version,
            PluginSourceResolution.expires_at > now,
        )
        .values(
            state="READY",
            state_version=expected_version + 1,
            content_hash=digest,
            storage_key=storage_key,
            byte_size=len(bundle.content),
            preview_json=preview,
            resolver_report_json=bundle.report,
            resolved_source_url=resolved.source,
            resolved_commit=resolved.commit,
            resolved_repo_path=resolved.repo_path,
            resolved_at=now,
            updated_at=now,
        )
        .returning(PluginSourceResolution.id)
    )
    if claimed is None:
        db.rollback()
        get_artifact_store().delete(storage_key)
        return
    finish(db)


def prepare_publish_resolution(
    db: Session, resolution_id: str, expected_state_version: int
) -> PluginSourcePublishPlan | dict[str, Any]:
    stmt = select(PluginSourceResolution).where(PluginSourceResolution.id == resolution_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if item is None:
        raise DomainError("PLUGIN_SOURCE_NOT_FOUND", "Plugin source resolution was not found", 404)
    if item.state == "PUBLISHED" and item.capability_version_id:
        if expected_state_version not in {item.state_version, item.state_version - 1}:
            raise DomainError(
                "PLUGIN_SOURCE_STATE_CONFLICT",
                "Plugin source was published at a different version",
                409,
                {"state": item.state, "state_version": item.state_version},
            )
        published = resolve_version(db, item.capability_version_id)
        return {
            **_read_model(item),
            "capability": {
                "capability_id": published.version.id,
                "capability_type": "PLUGIN",
                "capability_key": published.package.capability_key,
                "normalized_config": published.runtime_config(),
            },
        }
    if item.state != "READY" or item.state_version != expected_state_version:
        raise DomainError(
            "PLUGIN_SOURCE_STATE_CONFLICT",
            "Plugin source is not ready at the expected version",
            409,
            {"state": item.state, "state_version": item.state_version},
        )
    if item.expires_at <= datetime.now(UTC):
        item.state = "EXPIRED"
        item.state_version += 1
        storage_key = item.storage_key
        if storage_key:
            register_commit_action(db, lambda key=storage_key: get_artifact_store().delete(key))
        finish(db)
        raise DomainError("PLUGIN_SOURCE_EXPIRED", "Plugin source resolution has expired", 409)
    if (
        not item.storage_key
        or not item.content_hash
        or item.byte_size is None
        or not item.resolved_source_url
        or not item.resolved_commit
    ):
        raise DomainError("PLUGIN_SOURCE_INVALID", "Plugin source has no frozen package", 409)
    return PluginSourcePublishPlan(
        resolution_id=item.id,
        expected_state_version=item.state_version,
        storage_key=item.storage_key,
        content_hash=item.content_hash,
        byte_size=item.byte_size,
        preview=dict(item.preview_json or {}),
    )


def verify_publish_source(plan: PluginSourcePublishPlan) -> bytes:
    """Read and validate the frozen object without holding a DB transaction."""

    try:
        content = get_artifact_store().read(plan.storage_key)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError(
            "PLUGIN_SOURCE_INVALID", "Plugin source package is unavailable", 409
        ) from exc
    if len(content) != plan.byte_size or hashlib.sha256(content).hexdigest() != plan.content_hash:
        raise DomainError("PLUGIN_SOURCE_INVALID", "Plugin source package digest drifted", 409)
    preview = validate_plugin_archive(content, "remote-plugin")
    if preview != plan.preview:
        raise DomainError("PLUGIN_SOURCE_INVALID", "Plugin source preview drifted", 409)
    return content


def confirm_publish_resolution(db: Session, plan: PluginSourcePublishPlan) -> dict[str, Any]:
    stmt = select(PluginSourceResolution).where(PluginSourceResolution.id == plan.resolution_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if item is None:
        raise DomainError("PLUGIN_SOURCE_NOT_FOUND", "Plugin source resolution was not found", 404)
    if item.state == "PUBLISHED" and item.capability_version_id:
        published = resolve_version(db, item.capability_version_id)
        return {
            **_read_model(item),
            "capability": {
                "capability_id": published.version.id,
                "capability_type": "PLUGIN",
                "capability_key": published.package.capability_key,
                "normalized_config": published.runtime_config(),
            },
        }
    if (
        item.state != "READY"
        or item.state_version != plan.expected_state_version
        or item.storage_key != plan.storage_key
        or item.content_hash != plan.content_hash
        or item.byte_size != plan.byte_size
        or dict(item.preview_json or {}) != plan.preview
    ):
        raise DomainError(
            "PLUGIN_SOURCE_STATE_CONFLICT",
            "Plugin source changed while publication was being verified",
            409,
            {"state": item.state, "state_version": item.state_version},
        )
    if item.expires_at <= datetime.now(UTC):
        item.state = "EXPIRED"
        item.state_version += 1
        storage_key = plan.storage_key
        register_commit_action(db, lambda key=storage_key: get_artifact_store().delete(key))
        finish(db)
        raise DomainError("PLUGIN_SOURCE_EXPIRED", "Plugin source resolution has expired", 409)

    existing_blob = db.scalar(
        select(CapabilityBlob).where(CapabilityBlob.content_hash == plan.content_hash)
    )
    storage_key = existing_blob.storage_key if existing_blob else plan.storage_key
    resolved_commit = item.resolved_commit
    if not resolved_commit:
        raise DomainError(
            "PLUGIN_SOURCE_INVALID",
            "Plugin source has no frozen commit",
            409,
        )
    now = datetime.now(UTC)
    imported = CapabilityImport(
        token_digest=hashlib.sha256(f"plugin-source-resolution:{item.id}".encode()).hexdigest(),
        capability_type="PLUGIN",
        filename=(
            f"marketplace-{item.marketplace_plugin_name}-{resolved_commit[:12]}.zip"
            if item.source_kind == "MARKETPLACE"
            else f"git-{item.requested_commit[:12]}.zip"
        ),
        content_hash=plan.content_hash,
        storage_key=storage_key,
        byte_size=plan.byte_size,
        preview_json=plan.preview,
        state="COMMITTED",
        expires_at=item.expires_at,
        consumed_at=now,
    )
    db.add(imported)
    db.flush()
    raw_entries: object = plan.preview.get("capabilities")
    entries = cast(list[object], raw_entries) if isinstance(raw_entries, list) else []
    if len(entries) != 1 or not isinstance(entries[0], dict):
        raise DomainError("PLUGIN_SOURCE_INVALID", "Plugin source publication is invalid", 422)
    entry = cast(dict[str, Any], entries[0])
    capability_key = str(entry.get("capability_key") or "")
    raw_config: object = entry.get("normalized_config")
    normalized = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
    digest = capability_version_digest("PLUGIN", capability_key, plan.content_hash, normalized)
    duplicate = db.scalar(select(CapabilityVersion).where(CapabilityVersion.digest == digest))
    if duplicate is not None:
        capability = resolve_version(db, duplicate.id)
    else:
        published = publish_import(db, imported)
        if len(published) != 1:
            raise DomainError("PLUGIN_SOURCE_INVALID", "Plugin source publication is invalid", 422)
        capability = published[0]
    db.add(
        CapabilityValidation(
            capability_version_id=capability.version.id,
            validator=(
                "flowweave-marketplace-plugin-v1"
                if item.source_kind == "MARKETPLACE"
                else "flowweave-git-plugin-v1"
            ),
            status="PASSED",
            report_json={
                "source_kind": item.source_kind,
                "source_resolution_id": item.id,
                "source_url": item.source_url,
                "requested_commit": item.requested_commit,
                "repo_path": item.repo_path or None,
                "marketplace_plugin_name": item.marketplace_plugin_name or None,
                "resolved_source_url": item.resolved_source_url,
                "resolved_commit": item.resolved_commit,
                "resolved_repo_path": item.resolved_repo_path,
                "resolver": item.resolver_report_json,
            },
        )
    )
    item.state = "PUBLISHED"
    item.state_version += 1
    item.capability_version_id = capability.version.id
    item.published_at = now
    duplicate_storage_key = plan.storage_key
    if existing_blob is not None and duplicate_storage_key != storage_key:
        register_commit_action(
            db, lambda key=duplicate_storage_key: get_artifact_store().delete(key)
        )
    # The READY aggregate owns its content-addressed object until this DB
    # transaction commits. A rollback must retain it so publication can retry.
    finish(db)
    return {
        **_read_model(item),
        "capability": {
            "capability_id": capability.version.id,
            "capability_type": "PLUGIN",
            "capability_key": capability.package.capability_key,
            "normalized_config": capability.runtime_config(),
        },
    }


def expire_resolution(db: Session, resolution_id: str) -> None:
    item = db.get(PluginSourceResolution, resolution_id)
    if (
        item is None
        or item.state in {"PUBLISHED", "EXPIRED"}
        or item.expires_at > datetime.now(UTC)
    ):
        return
    item.state = "EXPIRED"
    item.state_version += 1
    storage_key = item.storage_key
    if storage_key:
        register_commit_action(db, lambda key=storage_key: get_artifact_store().delete(key))
    finish(db)


def record_resolution_failure(db: Session, resolution_id: str, error: str) -> None:
    item = db.get(PluginSourceResolution, resolution_id)
    if item is None or item.state != "PENDING":
        return
    item.state = "FAILED"
    item.state_version += 1
    item.error_detail = error[:2000]
    finish(db)
