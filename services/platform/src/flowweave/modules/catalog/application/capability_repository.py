from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.memory_sources import register_policy_references
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.domain.runtime_policy import (
    DEFAULT_CONTEXT_POLICY_CONFIG,
    DEFAULT_CONTEXT_POLICY_KEY,
    DEFAULT_CRITIC_POLICY_CONFIG,
    DEFAULT_CRITIC_POLICY_KEY,
    DEFAULT_MEMORY_POLICY_CONFIG,
    DEFAULT_MEMORY_POLICY_KEY,
)
from flowweave.shared.domain.tool_policy import (
    DEFAULT_TOOL_POLICY_CONFIG,
    DEFAULT_TOOL_POLICY_KEY,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AgentWorkspaceCapability,
    CapabilityBlob,
    CapabilityDependency,
    CapabilityImport,
    CapabilityPackage,
    CapabilityValidation,
    CapabilityVersion,
)


@dataclass(frozen=True, slots=True)
class PublishedCapability:
    package: CapabilityPackage
    version: CapabilityVersion
    blob: CapabilityBlob

    def runtime_config(self) -> dict[str, Any]:
        return {
            **self.version.normalized_config_json,
            "capability_id": self.version.id,
            "capability_version_id": self.version.id,
            "package_id": self.package.id,
            "version_no": self.version.version_no,
            "digest": self.version.digest,
            "filename": self.version.source_filename,
            "content_hash": self.blob.content_hash,
            "storage_key": self.blob.storage_key,
        }


def _builtin_id(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"flowweave:{kind}:{value}"))


def ensure_default_tool_policy(db: Session) -> PublishedCapability:
    """Return the current immutable built-in Tool Policy.

    Application code may repair a freshly constructed test database, but it
    never synthesizes policy inside a Runtime request. Every node and Snapshot
    references this concrete repository Version.
    """

    content = json.dumps(
        DEFAULT_TOOL_POLICY_CONFIG,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    blob_id = _builtin_id("blob", content_hash)
    package_id = _builtin_id("package", f"TOOL_POLICY:{DEFAULT_TOOL_POLICY_KEY}")
    digest = version_digest(
        "TOOL_POLICY", DEFAULT_TOOL_POLICY_KEY, content_hash, DEFAULT_TOOL_POLICY_CONFIG
    )
    blob = db.get(CapabilityBlob, blob_id)
    if blob is None:
        blob = CapabilityBlob(
            id=blob_id,
            content_hash=content_hash,
            storage_key=f"builtin://tool-policies/{content_hash}.json",
            byte_size=len(content),
            media_type="application/json",
        )
        db.add(blob)
    package = db.get(CapabilityPackage, package_id)
    if package is None:
        package = CapabilityPackage(
            id=package_id,
            capability_type="TOOL_POLICY",
            capability_key=DEFAULT_TOOL_POLICY_KEY,
            display_name="FlowWeave Default Tools",
            description=str(DEFAULT_TOOL_POLICY_CONFIG["description"]),
        )
        db.add(package)
    # The mappings intentionally do not expose cross-aggregate ORM
    # relationships. Flush repository parents explicitly so PostgreSQL can
    # enforce the immutable Version foreign keys without relying on ORM
    # instance dependency ordering.
    db.flush()
    version = db.scalar(
        select(CapabilityVersion).where(
            CapabilityVersion.package_id == package_id,
            CapabilityVersion.digest == digest,
        )
    )
    if version is None:
        # Empty databases may already have the compatible content in v3 because
        # historical migrations import the current frozen document.  Deployed
        # databases instead retain a provenance-drifted v3, so the v4 migration
        # publishes this explicit immutable successor before application code
        # can select it.
        version_id = _builtin_id("version", f"builtin:{DEFAULT_TOOL_POLICY_KEY}:4")
        version = CapabilityVersion(
            id=version_id,
            package_id=package_id,
            blob_id=blob_id,
            version_no=4,
            digest=digest,
            normalized_config_json=dict(DEFAULT_TOOL_POLICY_CONFIG),
            source_filename="flowweave-default-tools-v4.json",
            state="PUBLISHED",
        )
        db.add(version)
        db.flush()
        db.add(
            CapabilityValidation(
                id=_builtin_id("validation", version_id),
                capability_version_id=version_id,
                validator="flowweave-builtin-v4",
                status="PASSED",
                report_json={
                    "builtin": True,
                    "openhands_version": "1.44.0",
                    "source_commit": DEFAULT_TOOL_POLICY_CONFIG["source_commit"],
                    "catalog_digest": DEFAULT_TOOL_POLICY_CONFIG["catalog_digest"],
                },
            )
        )
    db.flush()
    return PublishedCapability(package, version, blob)


def ensure_context_policy(db: Session) -> PublishedCapability:
    """Install the immutable, fail-closed default OpenHands AgentContext."""

    config = dict(DEFAULT_CONTEXT_POLICY_CONFIG)
    content = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    key = DEFAULT_CONTEXT_POLICY_KEY
    blob_id = _builtin_id("blob", content_hash)
    package_id = _builtin_id("package", f"CONTEXT_POLICY:{key}")
    version_id = _builtin_id("version", f"builtin:{key}:1")
    digest = version_digest("CONTEXT_POLICY", key, content_hash, config)
    blob = db.get(CapabilityBlob, blob_id)
    if blob is None:
        blob = CapabilityBlob(
            id=blob_id,
            content_hash=content_hash,
            storage_key=f"builtin://context-policies/{content_hash}.json",
            byte_size=len(content),
            media_type="application/json",
        )
        db.add(blob)
    package = db.get(CapabilityPackage, package_id)
    if package is None:
        package = CapabilityPackage(
            id=package_id,
            capability_type="CONTEXT_POLICY",
            capability_key=key,
            display_name="FlowWeave Default Context",
            description=str(config.get("description") or ""),
        )
        db.add(package)
    db.flush()
    version = db.get(CapabilityVersion, version_id)
    if version is None:
        version = CapabilityVersion(
            id=version_id,
            package_id=package_id,
            blob_id=blob_id,
            version_no=1,
            digest=digest,
            normalized_config_json=config,
            source_filename=f"{key}.json",
            state="PUBLISHED",
        )
        db.add(version)
        db.flush()
        db.add(
            CapabilityValidation(
                id=_builtin_id("validation", version_id),
                capability_version_id=version_id,
                validator="flowweave-builtin-v1",
                status="PASSED",
                report_json={
                    "builtin": True,
                    "openhands_version": "1.40.0",
                    "memory_enabled": False,
                },
            )
        )
    db.flush()
    return PublishedCapability(package, version, blob)


def _ensure_builtin_policy(
    db: Session,
    *,
    capability_type: str,
    key: str,
    config: dict[str, Any],
    display_name: str,
) -> PublishedCapability:
    content = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    blob_id = _builtin_id("blob", content_hash)
    package_id = _builtin_id("package", f"{capability_type}:{key}")
    version_id = _builtin_id("version", f"builtin:{key}:1")
    digest = version_digest(capability_type, key, content_hash, config)
    blob = db.get(CapabilityBlob, blob_id)
    if blob is None:
        blob = CapabilityBlob(
            id=blob_id,
            content_hash=content_hash,
            storage_key=f"builtin://runtime-policies/{content_hash}.json",
            byte_size=len(content),
            media_type="application/json",
        )
        db.add(blob)
    package = db.get(CapabilityPackage, package_id)
    if package is None:
        package = CapabilityPackage(
            id=package_id,
            capability_type=capability_type,
            capability_key=key,
            display_name=display_name,
            description=str(config.get("description") or ""),
        )
        db.add(package)
    db.flush()
    version = db.get(CapabilityVersion, version_id)
    if version is None:
        version = CapabilityVersion(
            id=version_id,
            package_id=package_id,
            blob_id=blob_id,
            version_no=1,
            digest=digest,
            normalized_config_json=dict(config),
            source_filename=f"{key}.json",
            state="PUBLISHED",
        )
        db.add(version)
        db.flush()
        db.add(
            CapabilityValidation(
                id=_builtin_id("validation", version_id),
                capability_version_id=version_id,
                validator="flowweave-builtin-v1",
                status="PASSED",
                report_json={
                    "builtin": True,
                    "openhands_version": "1.40.0",
                    "enabled": False,
                },
            )
        )
    db.flush()
    return PublishedCapability(package, version, blob)


def ensure_default_memory_policy(db: Session) -> PublishedCapability:
    return _ensure_builtin_policy(
        db,
        capability_type="MEMORY_POLICY",
        key=DEFAULT_MEMORY_POLICY_KEY,
        config=DEFAULT_MEMORY_POLICY_CONFIG,
        display_name="FlowWeave Memory Disabled",
    )


def ensure_default_critic_policy(db: Session) -> PublishedCapability:
    return _ensure_builtin_policy(
        db,
        capability_type="CRITIC_POLICY",
        key=DEFAULT_CRITIC_POLICY_KEY,
        config=DEFAULT_CRITIC_POLICY_CONFIG,
        display_name="FlowWeave Critic Disabled",
    )


def version_digest(
    capability_type: str,
    capability_key: str,
    content_hash: str,
    normalized_config: dict[str, Any],
) -> str:
    return capability_version_digest(
        capability_type, capability_key, content_hash, normalized_config
    )


def _blob(db: Session, imported: CapabilityImport) -> CapabilityBlob:
    blob = db.scalar(
        select(CapabilityBlob).where(CapabilityBlob.content_hash == imported.content_hash)
    )
    if blob is not None:
        if blob.byte_size != imported.byte_size:
            raise DomainError(
                "CAPABILITY_BLOB_CONFLICT",
                "Capability content hash resolves to different immutable bytes",
                409,
            )
        return blob
    blob = CapabilityBlob(
        content_hash=imported.content_hash,
        storage_key=imported.storage_key,
        byte_size=imported.byte_size,
        media_type=(
            "application/zip"
            if imported.capability_type in {"SKILL", "PLUGIN"}
            else "application/json"
        ),
    )
    db.add(blob)
    db.flush()
    return blob


def _package(
    db: Session, capability_type: str, capability_key: str, normalized: dict[str, Any]
) -> CapabilityPackage:
    package = db.scalar(
        select(CapabilityPackage)
        .where(
            CapabilityPackage.capability_type == capability_type,
            CapabilityPackage.capability_key == capability_key,
        )
        .with_for_update()
    )
    if package is not None:
        return package
    package = CapabilityPackage(
        capability_type=capability_type,
        capability_key=capability_key,
        display_name=capability_key,
        description=str(normalized.get("description") or ""),
    )
    db.add(package)
    db.flush()
    return package


def publish_import(db: Session, imported: CapabilityImport) -> list[PublishedCapability]:
    """Publish each validated entry as an immutable repository version."""

    blob = _blob(db, imported)
    published: list[PublishedCapability] = []
    raw_entries: object = imported.preview_json.get("capabilities", [])
    entries = cast(list[object], raw_entries) if isinstance(raw_entries, list) else []
    for position, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise DomainError(
                "CAPABILITY_IMPORT_INVALID",
                "Capability import contains an invalid entry",
                422,
            )
        entry = cast(dict[str, Any], raw_entry)
        capability_key = str(entry.get("capability_key") or "")
        if not capability_key:
            raise DomainError(
                "CAPABILITY_IMPORT_INVALID",
                "Capability import entry has no stable key",
                422,
            )
        raw_config: object = entry.get("normalized_config", {})
        normalized = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
        package = _package(db, imported.capability_type, capability_key, normalized)
        existing = db.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.source_import_id == imported.id,
                CapabilityVersion.source_position == position,
            )
        )
        if existing is not None:
            published.append(PublishedCapability(package, existing, blob))
            continue
        next_version = (
            db.scalar(
                select(func.max(CapabilityVersion.version_no)).where(
                    CapabilityVersion.package_id == package.id
                )
            )
            or 0
        ) + 1
        digest = version_digest(
            imported.capability_type, capability_key, imported.content_hash, normalized
        )
        duplicate = db.scalar(select(CapabilityVersion).where(CapabilityVersion.digest == digest))
        if duplicate is not None:
            # RETIRED is a repository availability state, not a content
            # tombstone. Re-importing the exact immutable identity republishes
            # the canonical Version without changing its provenance or digest.
            duplicate.state = "PUBLISHED"
            published.append(PublishedCapability(package, duplicate, blob))
            continue
        version = CapabilityVersion(
            package_id=package.id,
            blob_id=blob.id,
            version_no=next_version,
            digest=digest,
            normalized_config_json=normalized,
            source_filename=imported.filename,
            source_import_id=imported.id,
            source_position=position,
            state="PUBLISHED",
        )
        db.add(version)
        db.flush()
        if imported.capability_type == "MEMORY_POLICY" and bool(normalized.get("enabled")):
            raw_refs = normalized.get("source_refs")
            if not isinstance(raw_refs, list):
                raise DomainError(
                    "MEMORY_POLICY_INVALID",
                    "Enabled Memory Policy has invalid frozen source references",
                    422,
                )
            source_refs: list[dict[str, str]] = []
            for raw_ref in cast(list[object], raw_refs):
                if not isinstance(raw_ref, dict):
                    raise DomainError(
                        "MEMORY_POLICY_INVALID",
                        "Enabled Memory Policy has invalid frozen source references",
                        422,
                    )
                source_refs.append(
                    {
                        str(key): str(value)
                        for key, value in cast(dict[object, object], raw_ref).items()
                    }
                )
            register_policy_references(
                db,
                policy_version_id=version.id,
                source_refs=source_refs,
            )
        raw_dependencies: object = normalized.get("dependencies", {})
        if isinstance(raw_dependencies, dict):
            dependencies = cast(dict[object, object], raw_dependencies)
            for ecosystem, raw_values in dependencies.items():
                if not isinstance(raw_values, dict):
                    continue
                values = cast(dict[object, object], raw_values)
                for name, pinned in values.items():
                    db.add(
                        CapabilityDependency(
                            capability_version_id=version.id,
                            ecosystem=str(ecosystem),
                            name=str(name),
                            version=str(pinned),
                        )
                    )
        db.add(
            CapabilityValidation(
                capability_version_id=version.id,
                validator="flowweave-import-v1",
                status="PASSED",
                report_json={"source_import_id": imported.id, "source_position": position},
            )
        )
        published.append(PublishedCapability(package, version, blob))
    return published


def publish_dependency_build(
    db: Session,
    source: PublishedCapability,
    normalized_config: dict[str, Any],
) -> tuple[PublishedCapability, bool]:
    """Publish dependency build output as a derived immutable version.

    The source version and all of its consumers remain unchanged. The returned
    boolean identifies whether this call inserted the derived version, allowing
    callers to safely reclaim newly written external artifacts on rollback.
    """

    digest = version_digest(
        source.package.capability_type,
        source.package.capability_key,
        source.blob.content_hash,
        normalized_config,
    )
    existing = db.scalar(select(CapabilityVersion).where(CapabilityVersion.digest == digest))
    if existing is not None:
        return PublishedCapability(source.package, existing, source.blob), False

    package = db.scalar(
        select(CapabilityPackage).where(CapabilityPackage.id == source.package.id).with_for_update()
    )
    if package is None:
        raise DomainError(
            "CAPABILITY_REPOSITORY_CORRUPT",
            "Capability package disappeared during dependency build",
            500,
        )
    next_version = (
        db.scalar(
            select(func.max(CapabilityVersion.version_no)).where(
                CapabilityVersion.package_id == package.id
            )
        )
        or 0
    ) + 1
    version = CapabilityVersion(
        package_id=package.id,
        blob_id=source.blob.id,
        version_no=next_version,
        digest=digest,
        normalized_config_json=normalized_config,
        source_filename=source.version.source_filename,
        source_import_id=None,
        source_position=None,
        state="PUBLISHED",
    )
    db.add(version)
    db.flush()
    raw_dependencies: object = normalized_config.get("dependencies", {})
    if isinstance(raw_dependencies, dict):
        for ecosystem, raw_values in cast(dict[object, object], raw_dependencies).items():
            if not isinstance(raw_values, dict):
                continue
            for name, pinned in cast(dict[object, object], raw_values).items():
                db.add(
                    CapabilityDependency(
                        capability_version_id=version.id,
                        ecosystem=str(ecosystem),
                        name=str(name),
                        version=str(pinned),
                    )
                )
    db.add(
        CapabilityValidation(
            capability_version_id=version.id,
            validator="flowweave-dependency-build-v1",
            status="PASSED",
            report_json={"derived_from_version_id": source.version.id},
        )
    )
    return PublishedCapability(package, version, source.blob), True


def publish_agent_profile_revision(
    db: Session,
    *,
    source: PublishedCapability,
    capability_key: str,
    normalized_config: dict[str, Any],
    validator: str,
    report: dict[str, Any],
) -> PublishedCapability:
    """Publish a derived immutable Agent Profile without rewriting consumers."""

    if source.package.capability_type != "AGENT_PROFILE":
        raise DomainError("AGENT_PROFILE_INVALID", "Source is not an Agent Profile", 422)
    package = _package(db, "AGENT_PROFILE", capability_key, normalized_config)
    digest = version_digest(
        "AGENT_PROFILE", capability_key, source.blob.content_hash, normalized_config
    )
    duplicate = db.scalar(select(CapabilityVersion).where(CapabilityVersion.digest == digest))
    if duplicate is not None:
        raise DomainError(
            "AGENT_PROFILE_VERSION_DUPLICATE",
            "An identical immutable Agent Profile Version already exists",
            409,
            {"capability_version_id": duplicate.id},
        )
    next_version = (
        db.scalar(
            select(func.max(CapabilityVersion.version_no)).where(
                CapabilityVersion.package_id == package.id
            )
        )
        or 0
    ) + 1
    version = CapabilityVersion(
        package_id=package.id,
        blob_id=source.blob.id,
        version_no=next_version,
        digest=digest,
        normalized_config_json=normalized_config,
        source_filename=source.version.source_filename,
        source_import_id=None,
        source_position=None,
        state="PUBLISHED",
    )
    db.add(version)
    db.flush()
    db.add(
        CapabilityValidation(
            capability_version_id=version.id,
            validator=validator,
            status="PASSED",
            report_json=report,
        )
    )
    db.flush()
    return PublishedCapability(package, version, source.blob)


def resolve_version(
    db: Session, capability_version_id: str, *, include_retired: bool = False
) -> PublishedCapability:
    version = db.get(CapabilityVersion, capability_version_id)
    if version is None or (version.state != "PUBLISHED" and not include_retired):
        raise DomainError(
            "CAPABILITY_REFERENCE_INVALID",
            "Capability version is unavailable",
            422,
            {"capability_version_id": capability_version_id},
        )
    package = db.get(CapabilityPackage, version.package_id)
    blob = db.get(CapabilityBlob, version.blob_id)
    if package is None or blob is None:
        raise DomainError(
            "CAPABILITY_REPOSITORY_CORRUPT",
            "Capability version is missing immutable repository data",
            500,
        )
    return PublishedCapability(package, version, blob)


def list_versions(db: Session) -> list[dict[str, Any]]:
    reference_rows = db.execute(
        select(AgentWorkspaceCapability.capability_version_id, func.count()).group_by(
            AgentWorkspaceCapability.capability_version_id
        )
    ).all()
    references: dict[str, int] = {version_id: int(count) for version_id, count in reference_rows}
    rows = db.execute(
        select(CapabilityVersion, CapabilityPackage, CapabilityBlob)
        .join(CapabilityPackage, CapabilityPackage.id == CapabilityVersion.package_id)
        .join(CapabilityBlob, CapabilityBlob.id == CapabilityVersion.blob_id)
        .where(CapabilityVersion.state == "PUBLISHED")
        .order_by(CapabilityVersion.created_at.desc(), CapabilityVersion.id.desc())
    ).all()
    latest_rows = db.execute(
        select(CapabilityVersion.package_id, func.max(CapabilityVersion.version_no))
        .where(CapabilityVersion.state == "PUBLISHED")
        .group_by(CapabilityVersion.package_id)
    ).all()
    latest: dict[str, int] = {
        package_id: int(version_no)
        for package_id, version_no in latest_rows
        if version_no is not None
    }
    result: list[dict[str, Any]] = []
    for version, package, blob in rows:
        normalized = cast(dict[str, Any], version.normalized_config_json or {})
        is_builtin = (
            package.capability_type == "TOOL_POLICY"
            and package.capability_key == DEFAULT_TOOL_POLICY_KEY
        )
        result.append(
            {
                "id": version.id,
                "lineage_id": package.id,
                "revision_number": version.version_no,
                "is_latest": latest.get(package.id) == version.version_no,
                "capability_type": package.capability_type,
                "capability_key": package.capability_key,
                "description": package.description,
                "version": str(normalized.get("version") or ""),
                "filename": version.source_filename,
                "content_hash": blob.content_hash,
                "digest": version.digest,
                "byte_size": blob.byte_size,
                "import_id": version.source_import_id,
                "created_at": version.created_at.isoformat(),
                "reference_count": int(references.get(version.id, 0)),
                "dependencies": normalized.get("dependencies", {}),
                "dependency_build_state": normalized.get("dependency_build_state", "NOT_REQUIRED"),
                "dependency_build_error": normalized.get("dependency_build_error"),
                "is_builtin": is_builtin,
                "document": normalized,
            }
        )
    return result
