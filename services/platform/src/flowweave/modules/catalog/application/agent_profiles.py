from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import (
    PublishedCapability,
    publish_agent_profile_revision,
    resolve_version,
)
from flowweave.shared.application.transactions import finish
from flowweave.shared.domain.runtime_policy import (
    OPENHANDS_AGENT_PROFILE_FIELD_MATRIX,
    OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION,
    normalize_agent_profile_document,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityVersion
from flowweave.shared.schemas import (
    AgentProfileCopyWrite,
    AgentProfileRetireWrite,
    AgentProfileRevisionWrite,
)


def _profile(db: Session, version_id: str, *, include_retired: bool = False) -> PublishedCapability:
    item = resolve_version(db, version_id, include_retired=include_retired)
    if item.package.capability_type != "AGENT_PROFILE":
        raise DomainError("AGENT_PROFILE_INVALID", "Capability is not an Agent Profile", 422)
    return item


def _read_model(item: PublishedCapability) -> dict[str, Any]:
    return {
        "id": item.version.id,
        "package_id": item.package.id,
        "capability_key": item.package.capability_key,
        "version_no": item.version.version_no,
        "digest": item.version.digest,
        "content_hash": item.blob.content_hash,
        "state": item.version.state,
        "document": dict(item.version.normalized_config_json),
        "compatibility": {
            "openhands_version": "1.44.0",
            "source_commit": "9a24f6c8866f353042a57df0514ccc900e3a0691",
            "schema_version": OPENHANDS_AGENT_PROFILE_SCHEMA_VERSION,
            "fields": dict(OPENHANDS_AGENT_PROFILE_FIELD_MATRIX),
            "server_profile_store": "PROHIBITED_FOR_PRODUCTION",
            "activation_semantics": "NEW_SNAPSHOT_AND_ATTEMPT",
        },
        "created_at": item.version.created_at.isoformat(),
    }


def read_profile(db: Session, version_id: str) -> dict[str, Any]:
    return _read_model(_profile(db, version_id, include_retired=True))


def list_profile_versions(db: Session, package_id: str) -> list[dict[str, Any]]:
    version_ids = list(
        db.scalars(
            select(CapabilityVersion.id)
            .where(CapabilityVersion.package_id == package_id)
            .order_by(CapabilityVersion.version_no.desc())
        )
    )
    result = [
        _read_model(_profile(db, version_id, include_retired=True)) for version_id in version_ids
    ]
    if result and any(item["package_id"] != package_id for item in result):
        raise DomainError("CAPABILITY_REPOSITORY_CORRUPT", "Profile lineage drifted", 500)
    return result


def revise_profile(
    db: Session, version_id: str, payload: AgentProfileRevisionWrite
) -> dict[str, Any]:
    source = _profile(db, version_id)
    if source.version.digest != payload.expected_digest:
        raise DomainError(
            "AGENT_PROFILE_VERSION_CONFLICT",
            "Agent Profile Version changed",
            409,
            {"expected": payload.expected_digest, "actual": source.version.digest},
        )
    try:
        key, normalized = normalize_agent_profile_document(
            payload.document, fallback_key=source.package.capability_key
        )
    except ValueError as exc:
        raise DomainError("AGENT_PROFILE_INVALID", str(exc), 422) from exc
    if key != source.package.capability_key:
        raise DomainError(
            "AGENT_PROFILE_IDENTITY_CHANGED",
            "A Profile revision cannot change its capability key; use copy instead",
            422,
        )
    revised = publish_agent_profile_revision(
        db,
        source=source,
        capability_key=key,
        normalized_config=normalized,
        validator="flowweave-agent-profile-v2",
        report={"operation": "REVISION", "source_version_id": source.version.id},
    )
    finish(db)
    return _read_model(revised)


def copy_profile(db: Session, version_id: str, payload: AgentProfileCopyWrite) -> dict[str, Any]:
    source = _profile(db, version_id, include_retired=True)
    document = dict(source.version.normalized_config_json)
    document["name"] = payload.capability_key
    document["source_profile_id"] = source.version.id
    document["source_revision"] = source.version.version_no
    try:
        key, normalized = normalize_agent_profile_document(
            document, fallback_key=payload.capability_key
        )
    except ValueError as exc:
        raise DomainError("AGENT_PROFILE_INVALID", str(exc), 422) from exc
    copied = publish_agent_profile_revision(
        db,
        source=source,
        capability_key=key,
        normalized_config=normalized,
        validator="flowweave-agent-profile-v2",
        report={"operation": "COPY", "source_version_id": source.version.id},
    )
    finish(db)
    return _read_model(copied)


def retire_profile(
    db: Session, version_id: str, payload: AgentProfileRetireWrite
) -> dict[str, Any]:
    source = _profile(db, version_id)
    if source.version.digest != payload.expected_digest:
        raise DomainError(
            "AGENT_PROFILE_VERSION_CONFLICT",
            "Agent Profile Version changed",
            409,
            {"expected": payload.expected_digest, "actual": source.version.digest},
        )
    source.version.state = "RETIRED"
    finish(db)
    return _read_model(source)
