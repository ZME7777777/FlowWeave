"""Repository browsing: order logical capabilities in SQL before paging versions."""

from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_collections import collection_projection
from flowweave.modules.catalog.application.capability_repository import list_versions
from flowweave.modules.catalog.application.platform_capabilities import (
    PLATFORM_MANAGED_CAPABILITY_KEYS,
)
from flowweave.shared.models import (
    CapabilityBlob,
    CapabilityCollection,
    CapabilityCollectionItem,
    CapabilityPackage,
    CapabilityVersion,
)

CapabilityModule = Literal["SKILL", "PLUGIN", "MCP", "AGENT_DEFINITION", "CONTEXT", "HOOK"]
CapabilitySort = Literal["created_at", "name", "byte_size", "revision_number"]
CollectionSort = Literal["created_at", "name", "category", "member_count", "updated_at"]
SortDirection = Literal["asc", "desc"]


def list_directory(
    db: Session,
    capability_type: CapabilityModule,
    q: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: CapabilitySort = "created_at",
    direction: SortDirection = "desc",
) -> dict[str, Any]:
    latest = (
        select(
            CapabilityVersion.package_id, func.max(CapabilityVersion.version_no).label("revision")
        )
        .where(CapabilityVersion.state == "PUBLISHED")
        .group_by(CapabilityVersion.package_id)
        .subquery()
    )
    public_package = ~CapabilityPackage.capability_key.in_(PLATFORM_MANAGED_CAPABILITY_KEYS)
    count_rows = db.execute(
        select(CapabilityPackage.capability_type, func.count())
        .join(latest, latest.c.package_id == CapabilityPackage.id)
        .where(public_package)
        .group_by(CapabilityPackage.capability_type)
    ).all()
    counts = {kind: count for kind, count in count_rows}
    query = (
        select(CapabilityPackage.id)
        .join(latest, latest.c.package_id == CapabilityPackage.id)
        .join(
            CapabilityVersion,
            (CapabilityVersion.package_id == CapabilityPackage.id)
            & (CapabilityVersion.version_no == latest.c.revision),
        )
        .join(CapabilityBlob, CapabilityBlob.id == CapabilityVersion.blob_id)
        .where(CapabilityPackage.capability_type == capability_type, public_package)
    )
    if q.strip():
        needle = f"%{q.strip().replace('/', '//').replace('%', '/%').replace('_', '/_')}%"
        query = query.where(
            or_(
                CapabilityPackage.capability_key.ilike(needle, escape="/"),
                CapabilityPackage.description.ilike(needle, escape="/"),
                CapabilityVersion.source_filename.ilike(needle, escape="/"),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    order = {
        "created_at": CapabilityVersion.created_at,
        "name": func.lower(CapabilityPackage.capability_key),
        "byte_size": CapabilityBlob.byte_size,
        "revision_number": CapabilityVersion.version_no,
    }[sort]
    ids = list(
        db.scalars(
            query.order_by(
                order.asc() if direction == "asc" else order.desc(), CapabilityPackage.id.asc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    versions = list_versions(db, ids)
    grouped: dict[str, list[dict[str, Any]]] = {package_id: [] for package_id in ids}
    for version in versions:
        grouped[version["lineage_id"]].append(version)
    return {
        "items": [
            {
                "id": package_id,
                "versions": grouped[package_id],
                "latest": next(item for item in grouped[package_id] if item["is_latest"]),
            }
            for package_id in ids
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": counts,
        "collection_total": db.scalar(select(func.count()).select_from(CapabilityCollection)) or 0,
    }


def list_collection_directory(
    db: Session,
    q: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: CollectionSort = "created_at",
    direction: SortDirection = "desc",
) -> dict[str, Any]:
    members = (
        select(func.count())
        .select_from(CapabilityCollectionItem)
        .where(CapabilityCollectionItem.collection_id == CapabilityCollection.id)
        .correlate(CapabilityCollection)
        .scalar_subquery()
    )
    query = select(CapabilityCollection)
    if q.strip():
        needle = f"%{q.strip().replace('/', '//').replace('%', '/%').replace('_', '/_')}%"
        query = query.where(
            or_(
                *(
                    column.ilike(needle, escape="/")
                    for column in (
                        CapabilityCollection.name,
                        CapabilityCollection.category,
                        CapabilityCollection.description,
                    )
                )
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    order = {
        "created_at": CapabilityCollection.created_at,
        "name": func.lower(CapabilityCollection.name),
        "category": CapabilityCollection.category,
        "member_count": members,
        "updated_at": CapabilityCollection.updated_at,
    }[sort]
    rows = list(
        db.scalars(
            query.order_by(
                order.asc() if direction == "asc" else order.desc(), CapabilityCollection.id.asc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    collection_ids = [item.id for item in rows]
    member_rows = list(
        db.scalars(
            select(CapabilityCollectionItem)
            .where(CapabilityCollectionItem.collection_id.in_(collection_ids))
            .order_by(CapabilityCollectionItem.position, CapabilityCollectionItem.id)
        )
    )
    package_ids = list(
        db.scalars(
            select(CapabilityVersion.package_id)
            .where(CapabilityVersion.id.in_([item.capability_version_id for item in member_rows]))
            .distinct()
        )
    )
    capabilities = {item["id"]: item for item in list_versions(db, package_ids)}
    grouped_members: dict[str, list[CapabilityCollectionItem]] = {item.id: [] for item in rows}
    for member in member_rows:
        grouped_members[member.collection_id].append(member)
    return {
        "items": [
            collection_projection(item, grouped_members[item.id], capabilities) for item in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
