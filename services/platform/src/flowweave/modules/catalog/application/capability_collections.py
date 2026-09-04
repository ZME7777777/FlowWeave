from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import (
    PublishedCapability,
    list_versions,
    resolve_version,
)
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    CapabilityCollection,
    CapabilityCollectionItem,
    CapabilityVersion,
)
from flowweave.shared.schemas import CapabilityCollectionWrite


def _time(value: datetime) -> str:
    return value.isoformat()


def _resolve_capability(db: Session, capability_id: str) -> PublishedCapability:
    return resolve_version(db, capability_id)


def refresh_skill_collection_members(
    db: Session, published_capabilities: Iterable[PublishedCapability]
) -> None:
    """Move logical Skill-selection templates to newly published versions.

    Collections are shortcuts for selecting the current version, rather than
    consumers whose historical identity needs to be frozen.  Other consumers
    (sessions, nodes, environments, and snapshots) deliberately retain their
    immutable version references.
    """

    replacements = {
        capability.package.id: capability.version.id
        for capability in published_capabilities
        if capability.package.capability_type == "SKILL"
    }
    if not replacements:
        return

    members = db.execute(
        select(CapabilityCollectionItem, CapabilityVersion)
        .join(
            CapabilityVersion,
            CapabilityVersion.id == CapabilityCollectionItem.capability_version_id,
        )
        .where(CapabilityVersion.package_id.in_(replacements))
    ).all()
    changed_collection_ids: set[str] = set()
    for member, version in members:
        latest_version_id = replacements[version.package_id]
        if member.capability_version_id == latest_version_id:
            continue
        member.capability_version_id = latest_version_id
        changed_collection_ids.add(member.collection_id)

    if not changed_collection_ids:
        return
    changed_at = datetime.now(UTC)
    for collection in db.scalars(
        select(CapabilityCollection).where(CapabilityCollection.id.in_(changed_collection_ids))
    ):
        collection.row_version += 1
        collection.updated_at = changed_at


def _collection_dict(db: Session, item: CapabilityCollection) -> dict[str, Any]:
    members = db.scalars(
        select(CapabilityCollectionItem)
        .where(CapabilityCollectionItem.collection_id == item.id)
        .order_by(CapabilityCollectionItem.position, CapabilityCollectionItem.id)
    ).all()
    capabilities = {value["id"]: value for value in list_versions(db)}
    resolved_members: list[dict[str, Any]] = []
    for member in members:
        capability_id = member.capability_version_id
        capability = capabilities.get(capability_id)
        if capability is None:
            raise DomainError(
                "CAPABILITY_COLLECTION_INVALID",
                "Capability collection contains an unavailable immutable version",
                409,
                {"collection_id": item.id, "capability_id": capability_id},
            )
        resolved_members.append(capability)
    return {
        "id": item.id,
        "name": item.name,
        "category": item.category,
        "description": item.description,
        "row_version": item.row_version,
        "members": resolved_members,
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


def list_collections(db: Session) -> list[dict[str, Any]]:
    return [
        _collection_dict(db, item)
        for item in db.scalars(
            select(CapabilityCollection).order_by(
                CapabilityCollection.category,
                CapabilityCollection.name,
                CapabilityCollection.id,
            )
        ).all()
    ]


def save_collection(
    db: Session, payload: CapabilityCollectionWrite, collection_id: str | None = None
) -> dict[str, Any]:
    resolved = [_resolve_capability(db, capability_id) for capability_id in payload.capability_ids]
    non_skill_ids = [
        item.version.id for item in resolved if item.package.capability_type != "SKILL"
    ]
    if non_skill_ids:
        raise DomainError(
            "SKILL_COLLECTION_TYPE_REQUIRED",
            "A Skill Collection can contain only Skill versions",
            422,
            {"capability_ids": non_skill_ids},
        )
    identities = [(item.package.capability_type, item.package.capability_key) for item in resolved]
    if len(identities) != len(set(identities)):
        raise DomainError(
            "CAPABILITY_COLLECTION_DUPLICATE_LINEAGE",
            "A Capability Collection cannot contain multiple versions of the same type and key",
            422,
        )
    if collection_id:
        item = db.get(CapabilityCollection, collection_id)
        if item is None:
            raise not_found("capability_collection", collection_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "Capability Collection was modified",
                expected=payload.row_version,
                actual=item.row_version,
            )
        item.row_version += 1
        item.updated_at = datetime.now(UTC)
        db.execute(
            delete(CapabilityCollectionItem).where(
                CapabilityCollectionItem.collection_id == collection_id
            )
        )
    else:
        item = CapabilityCollection(
            name=payload.name,
            category=payload.category,
            description=payload.description,
        )
        db.add(item)
        db.flush()
    item.name = payload.name
    item.category = payload.category
    item.description = payload.description
    for position, published in enumerate(resolved):
        db.add(
            CapabilityCollectionItem(
                collection_id=item.id,
                capability_version_id=published.version.id,
                position=position,
            )
        )
    try:
        db.flush()
    except IntegrityError as exc:
        raise DomainError(
            "CAPABILITY_COLLECTION_NAME_CONFLICT",
            "Capability Collection name already exists",
            409,
        ) from exc
    result = _collection_dict(db, item)
    finish(db)
    return result


def delete_collection(db: Session, collection_id: str) -> None:
    item = db.get(CapabilityCollection, collection_id)
    if item is None:
        raise not_found("capability_collection", collection_id)
    db.delete(item)
    finish(db)
