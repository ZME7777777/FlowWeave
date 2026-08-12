from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_imports import list_capabilities
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    CapabilityImport,
    SkillCollection,
    SkillCollectionItem,
)
from flowweave.shared.schemas import SkillCollectionWrite


def _time(value: datetime) -> str:
    return value.isoformat()


def _resolve_skill(db: Session, capability_id: str) -> tuple[CapabilityImport, int, dict[str, Any]]:
    import_id, separator, raw_position = capability_id.rpartition(":")
    if not separator or not raw_position.isdigit():
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Skill version is invalid", 422)
    imported = db.get(CapabilityImport, import_id)
    position = int(raw_position)
    entries: list[object] = (
        cast(list[object], imported.preview_json.get("capabilities", []))
        if imported and isinstance(imported.preview_json.get("capabilities", []), list)
        else []
    )
    if (
        imported is None
        or imported.state != "COMMITTED"
        or imported.capability_type != "SKILL"
        or position >= len(entries)
        or not isinstance(entries[position], dict)
        or cast(dict[str, Any], entries[position]).get("deleted_at")
    ):
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Skill version is unavailable", 422)
    return imported, position, cast(dict[str, Any], entries[position])


def _collection_dict(db: Session, item: SkillCollection) -> dict[str, Any]:
    members = db.scalars(
        select(SkillCollectionItem)
        .where(SkillCollectionItem.collection_id == item.id)
        .order_by(SkillCollectionItem.position, SkillCollectionItem.id)
    ).all()
    capabilities = {value["id"]: value for value in list_capabilities(db)}
    resolved_members: list[dict[str, Any]] = []
    for member in members:
        capability_id = f"{member.capability_import_id}:{member.capability_position}"
        capability = capabilities.get(capability_id)
        if capability is None:
            raise DomainError(
                "SKILL_COLLECTION_INVALID",
                "Skill collection contains an unavailable Skill version",
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
            select(SkillCollection).order_by(
                SkillCollection.category, SkillCollection.name, SkillCollection.id
            )
        ).all()
    ]


def save_collection(
    db: Session, payload: SkillCollectionWrite, collection_id: str | None = None
) -> dict[str, Any]:
    resolved = [_resolve_skill(db, capability_id) for capability_id in payload.capability_ids]
    capability_keys = [
        str(entry.get("capability_key") or "") for _item, _position, entry in resolved
    ]
    if len(capability_keys) != len(set(capability_keys)):
        raise DomainError(
            "SKILL_COLLECTION_DUPLICATE_SKILL",
            "A Skill collection cannot contain multiple versions of the same Skill",
            422,
        )
    if collection_id:
        item = db.get(SkillCollection, collection_id)
        if item is None:
            raise not_found("skill_collection", collection_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "Skill collection was modified",
                expected=payload.row_version,
                actual=item.row_version,
            )
        item.row_version += 1
        item.updated_at = datetime.now(UTC)
        db.execute(
            delete(SkillCollectionItem).where(SkillCollectionItem.collection_id == collection_id)
        )
    else:
        item = SkillCollection(
            name=payload.name,
            category=payload.category,
            description=payload.description,
        )
        db.add(item)
        db.flush()
    item.name = payload.name
    item.category = payload.category
    item.description = payload.description
    for position, (imported, capability_position, _entry) in enumerate(resolved):
        db.add(
            SkillCollectionItem(
                collection_id=item.id,
                capability_import_id=imported.id,
                capability_position=capability_position,
                position=position,
            )
        )
    try:
        db.flush()
    except IntegrityError as exc:
        raise DomainError(
            "SKILL_COLLECTION_NAME_CONFLICT",
            "Skill collection name already exists",
            409,
        ) from exc
    result = _collection_dict(db, item)
    finish(db)
    return result


def delete_collection(db: Session, collection_id: str) -> None:
    item = db.get(SkillCollection, collection_id)
    if item is None:
        raise not_found("skill_collection", collection_id)
    db.delete(item)
    finish(db)
