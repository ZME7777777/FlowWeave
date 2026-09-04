from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.runtime.workspace import (
    cleanup_node_workspace,
    materialize_node_workspace,
    node_workspace_relative,
)
from flowweave.shared.application.transactions import finish, register_commit_action
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    CapabilityBlob,
    CapabilityPackage,
    CapabilityVersion,
    FlowDefinition,
    FlowNode,
    NodeAsset,
    NodeContextCapability,
    NodeDirectory,
    NodeExecutorConfig,
    NodeIOField,
)
from flowweave.shared.schemas import DirectoryWrite, NodeAssetWrite


class FlowReference(TypedDict):
    id: str
    name: str
    reference_count: int


class BlockedAsset(TypedDict):
    id: str
    name: str
    flows: list[FlowReference]


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def directory_dict(item: NodeDirectory) -> dict[str, Any]:
    return {
        "id": item.id,
        "parent_id": item.parent_id,
        "name": item.name,
        "position": item.position,
        "row_version": item.row_version,
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


def asset_dict(db: Session, item: NodeAsset) -> dict[str, Any]:
    fields = db.scalars(
        select(NodeIOField)
        .where(NodeIOField.node_asset_id == item.id)
        .order_by(NodeIOField.direction, NodeIOField.position)
    ).all()
    executor = db.get(NodeExecutorConfig, item.id)
    contexts = db.execute(
        select(NodeContextCapability, CapabilityVersion, CapabilityPackage, CapabilityBlob)
        .join(
            CapabilityVersion,
            CapabilityVersion.id == NodeContextCapability.capability_version_id,
        )
        .join(CapabilityPackage, CapabilityPackage.id == CapabilityVersion.package_id)
        .join(CapabilityBlob, CapabilityBlob.id == CapabilityVersion.blob_id)
        .where(NodeContextCapability.node_asset_id == item.id)
        .order_by(NodeContextCapability.position)
    ).all()

    def field_dict(x: NodeIOField) -> dict[str, Any]:
        return {
            "id": x.id,
            "field_key": x.field_key,
            "display_name": x.display_name,
            "data_type": x.data_type,
            "description": x.description,
            "position": x.position,
        }

    def context_text(version: CapabilityVersion) -> str:
        config = version.normalized_config_json or {}
        return str(config.get("text") or "")

    return {
        "id": item.id,
        "directory_id": item.directory_id,
        "name": item.name,
        "description": item.description,
        "icon_kind": item.icon_kind,
        "icon_value": item.icon_value,
        "workspace_ref": str(node_workspace_relative(item.id)),
        "row_version": item.row_version,
        "inputs": [field_dict(x) for x in fields if x.direction == "INPUT"],
        "outputs": [field_dict(x) for x in fields if x.direction == "OUTPUT"],
        "executor": {
            "startup_prompt": executor.startup_prompt,
            "context_prompt": executor.context_prompt,
            "context_capability_ids": [
                reference.capability_version_id for reference, _, _, _ in contexts
            ],
        }
        if executor
        else None,
        "context_capabilities": [
            {
                "id": version.id,
                "capability_key": package.capability_key,
                "digest": version.digest,
                "content_hash": blob.content_hash,
                "text": context_text(version),
            }
            for _, version, package, blob in contexts
        ],
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


def list_directories(db: Session) -> list[dict[str, Any]]:
    return [
        directory_dict(x)
        for x in db.scalars(
            select(NodeDirectory).order_by(NodeDirectory.position, NodeDirectory.name)
        )
    ]


def create_directory(db: Session, payload: DirectoryWrite) -> dict[str, Any]:
    if payload.parent_id and not db.get(NodeDirectory, payload.parent_id):
        raise not_found("node_directory", payload.parent_id)
    item = NodeDirectory(**payload.model_dump())
    db.add(item)
    finish(db)
    return directory_dict(item)


def delete_directories(db: Session, directory_ids: list[str]) -> dict[str, Any]:
    """Delete selected directory trees while retaining their node assets.

    Assets are deliberately moved to the unclassified collection rather than
    deleted as a side effect of organizing the catalog.
    """
    requested = set(directory_ids)
    items = db.scalars(
        select(NodeDirectory).where(NodeDirectory.id.in_(requested)).with_for_update()
    ).all()
    found = {item.id for item in items}
    missing = next((item_id for item_id in requested if item_id not in found), None)
    if missing is not None:
        raise not_found("node_directory", missing)

    all_directories = db.scalars(select(NodeDirectory).with_for_update()).all()
    by_parent: dict[str | None, list[str]] = {}
    for item in all_directories:
        by_parent.setdefault(item.parent_id, []).append(item.id)

    removed: set[str] = set()

    def collect(directory_id: str) -> None:
        if directory_id in removed:
            return
        removed.add(directory_id)
        for child_id in by_parent.get(directory_id, []):
            collect(child_id)

    for item_id in requested:
        collect(item_id)
    db.execute(update(NodeAsset).where(NodeAsset.directory_id.in_(removed)).values(directory_id=None))
    db.execute(delete(NodeDirectory).where(NodeDirectory.id.in_(removed)))
    finish(db)
    return {"deleted_ids": sorted(removed)}


def delete_directory(db: Session, directory_id: str) -> None:
    """Remove a directory while retaining its direct children and assets.

    Direct children are promoted to the deleted directory's parent and assets
    become members of that same parent.  Refuse promotion on a name collision
    rather than silently merging independent directory identities.
    """
    item = db.scalar(
        select(NodeDirectory).where(NodeDirectory.id == directory_id).with_for_update()
    )
    if item is None:
        raise not_found("node_directory", directory_id)
    children = db.scalars(
        select(NodeDirectory).where(NodeDirectory.parent_id == item.id).with_for_update()
    ).all()
    for child in children:
        conflict_id = db.scalar(
            select(NodeDirectory.id).where(
                NodeDirectory.parent_id == item.parent_id,
                NodeDirectory.name == child.name,
                NodeDirectory.id != child.id,
            )
        )
        if conflict_id is not None:
            raise DomainError(
                "NODE_DIRECTORY_PROMOTION_CONFLICT",
                "删除后父目录中会出现同名子目录，请先调整目录名称或层级。",
                409,
                {"directory_id": item.id, "child_directory_id": child.id, "name": child.name},
            )
    for child in children:
        child.parent_id = item.parent_id
    for asset in db.scalars(
        select(NodeAsset).where(NodeAsset.directory_id == item.id).with_for_update()
    ):
        asset.directory_id = item.parent_id
        asset.row_version += 1
        asset.updated_at = datetime.now(UTC)
    db.delete(item)
    finish(db)


def list_assets(
    db: Session, directory_id: str | None = None, query: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(NodeAsset)
    if directory_id:
        stmt = stmt.where(NodeAsset.directory_id == directory_id)
    if query:
        stmt = stmt.where(NodeAsset.name.ilike(f"%{query}%"))
    result = [asset_dict(db, x) for x in db.scalars(stmt.order_by(NodeAsset.updated_at.desc()))]
    for asset in result:
        materialize_node_workspace(asset)
    return result


def read_asset(db: Session, asset_id: str) -> dict[str, Any]:
    result = asset_dict(db, get_asset(db, asset_id))
    materialize_node_workspace(result)
    return result


def get_asset(db: Session, asset_id: str) -> NodeAsset:
    item = db.get(NodeAsset, asset_id)
    if not item:
        raise not_found("node_asset", asset_id)
    return item


def _replace_children(db: Session, item: NodeAsset, payload: NodeAssetWrite) -> None:
    db.execute(delete(NodeIOField).where(NodeIOField.node_asset_id == item.id))
    db.execute(delete(NodeExecutorConfig).where(NodeExecutorConfig.node_asset_id == item.id))
    db.execute(delete(NodeContextCapability).where(NodeContextCapability.node_asset_id == item.id))
    for direction, fields in (("INPUT", payload.inputs), ("OUTPUT", payload.outputs)):
        for position, field in enumerate(fields):
            db.add(
                NodeIOField(
                    node_asset_id=item.id,
                    direction=direction,
                    position=position,
                    **field.model_dump(),
                )
            )
    db.add(
        NodeExecutorConfig(
            node_asset_id=item.id,
            startup_prompt=payload.executor.startup_prompt,
            context_prompt=payload.executor.context_prompt,
        )
    )
    for position, version_id in enumerate(payload.executor.context_capability_ids):
        version = db.get(CapabilityVersion, version_id)
        package = db.get(CapabilityPackage, version.package_id) if version else None
        if (
            version is None
            or package is None
            or version.state != "PUBLISHED"
            or package.capability_type != "CONTEXT"
        ):
            raise DomainError("NODE_CONTEXT_INVALID", "节点只能选择已发布的 Context 版本", 422)
        db.add(
            NodeContextCapability(
                node_asset_id=item.id,
                capability_version_id=version.id,
                position=position,
            )
        )


def save_asset(db: Session, payload: NodeAssetWrite, asset_id: str | None = None) -> dict[str, Any]:
    if payload.directory_id and not db.get(NodeDirectory, payload.directory_id):
        raise not_found("node_directory", payload.directory_id)
    if asset_id:
        item = get_asset(db, asset_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "node asset was modified", expected=payload.row_version, actual=item.row_version
            )
        item.row_version += 1
        item.updated_at = datetime.now(UTC)
    else:
        item = None

    duplicate_id = db.scalar(
        select(NodeAsset.id)
        .where(
            NodeAsset.directory_id == payload.directory_id,
            NodeAsset.name == payload.name,
            *((NodeAsset.id != asset_id,) if asset_id is not None else ()),
        )
        .limit(1)
    )
    if duplicate_id is not None:
        raise DomainError(
            "NODE_ASSET_NAME_CONFLICT",
            "当前目录已存在同名节点资产，请使用其他名称。",
            409,
            {"directory_id": payload.directory_id, "name": payload.name},
        )

    if item is None:
        item = NodeAsset(
            directory_id=payload.directory_id,
            name=payload.name,
            description=payload.description,
            icon_kind=payload.icon_kind,
            icon_value=payload.icon_value,
        )
        db.add(item)
        # AsyncSession compatibility uses autoflush=False. Materialize the
        # parent key before constructing child rows in the same transaction.
        db.flush()
    for key in (
        "directory_id",
        "name",
        "description",
        "icon_kind",
        "icon_value",
    ):
        setattr(item, key, getattr(payload, key))
    _replace_children(db, item, payload)
    db.flush()
    result = asset_dict(db, item)
    materialize_node_workspace(result)
    finish(db)
    return result


def delete_assets(db: Session, asset_ids: list[str]) -> dict[str, Any]:
    ids = sorted(set(asset_ids))
    items = db.scalars(
        select(NodeAsset).where(NodeAsset.id.in_(ids)).order_by(NodeAsset.id).with_for_update()
    ).all()
    items_by_id = {item.id: item for item in items}
    missing = next((asset_id for asset_id in ids if asset_id not in items_by_id), None)
    if missing is not None:
        raise not_found("node_asset", missing)

    references: dict[str, list[FlowReference]] = {asset_id: [] for asset_id in ids}
    rows = db.execute(
        select(
            FlowNode.node_asset_id,
            FlowDefinition.id,
            FlowDefinition.name,
            func.count(FlowNode.id),
        )
        .join(FlowDefinition, FlowDefinition.id == FlowNode.flow_id)
        .where(FlowNode.node_asset_id.in_(ids))
        .group_by(FlowNode.node_asset_id, FlowDefinition.id, FlowDefinition.name)
        .order_by(FlowDefinition.name, FlowDefinition.id)
    ).tuples()
    for asset_id, flow_id, flow_name, reference_count in rows:
        references[asset_id].append(
            {
                "id": flow_id,
                "name": flow_name,
                "reference_count": reference_count,
            }
        )

    blocked: list[BlockedAsset] = [
        {
            "id": item.id,
            "name": item.name,
            "flows": references[item.id],
        }
        for item in items
        if references[item.id]
    ]
    blocked_ids = {item["id"] for item in blocked}
    deleted_ids: list[str] = []
    for item in items:
        if item.id in blocked_ids:
            continue
        db.delete(item)
        register_commit_action(db, lambda asset_id=item.id: cleanup_node_workspace(asset_id))
        deleted_ids.append(item.id)
    finish(db)
    return {
        "deleted_ids": deleted_ids,
        "blocked": [{**item, "relation": "FLOW_NODE"} for item in blocked],
    }


def delete_asset(db: Session, asset_id: str) -> None:
    result = delete_assets(db, [asset_id])
    if result["blocked"]:
        raise DomainError(
            "NODE_ASSET_IN_USE",
            "Node asset is referenced by active flows",
            409,
            {"assets": result["blocked"]},
        )
