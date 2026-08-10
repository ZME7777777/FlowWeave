from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from flowweave.modules.environments.public import lock_referenceable_version
from flowweave.runtime.workspace import materialize_node_workspace, node_workspace_relative
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
    CapabilityImport,
    EnvironmentVersion,
    FlowDefinition,
    FlowNode,
    ModelProvider,
    NodeAsset,
    NodeCapabilityRef,
    NodeDirectory,
    NodeExecutorConfig,
    NodeIOField,
    ProviderModel,
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
    capabilities = db.scalars(
        select(NodeCapabilityRef)
        .where(NodeCapabilityRef.node_asset_id == item.id)
        .order_by(NodeCapabilityRef.position)
    ).all()
    environment = (
        db.get(EnvironmentVersion, item.environment_version_id)
        if item.environment_version_id
        else None
    )

    def field_dict(x: NodeIOField) -> dict[str, Any]:
        return {
            "id": x.id,
            "field_key": x.field_key,
            "display_name": x.display_name,
            "data_type": x.data_type,
            "description": x.description,
            "template_url": x.template_url,
            "position": x.position,
        }

    return {
        "id": item.id,
        "directory_id": item.directory_id,
        "name": item.name,
        "description": item.description,
        "icon_kind": item.icon_kind,
        "icon_value": item.icon_value,
        "workspace_ref": str(node_workspace_relative(item.id)),
        "environment_version_id": item.environment_version_id,
        "environment_version": (
            {
                "id": environment.id,
                "environment_id": environment.environment_id,
                "version_no": environment.version_no,
                "state": environment.state,
                "image_reference": environment.image_reference,
                "image_digest": environment.image_digest,
                "manifest": environment.manifest_json or {},
            }
            if environment
            else None
        ),
        "row_version": item.row_version,
        "inputs": [field_dict(x) for x in fields if x.direction == "INPUT"],
        "outputs": [field_dict(x) for x in fields if x.direction == "OUTPUT"],
        "executor": {
            "model_provider_id": executor.model_provider_id,
            "model_name": executor.model_name,
            "startup_prompt": executor.startup_prompt,
            "context_prompt": executor.context_prompt,
            "timeout_seconds": executor.timeout_seconds,
            "max_iterations": executor.max_iterations,
        }
        if executor
        else None,
        "capabilities": [
            {
                "id": x.id,
                "capability_id": _capability_id(db, x),
                "capability_type": x.capability_type,
                "capability_key": x.capability_key,
                "normalized_config": x.normalized_config,
                "position": x.position,
            }
            for x in capabilities
        ],
        "created_at": _time(item.created_at),
        "updated_at": _time(item.updated_at),
    }


def _capability_id(db: Session, capability: NodeCapabilityRef) -> str | None:
    stored = capability.normalized_config.get("capability_id")
    if isinstance(stored, str):
        return stored
    import_id = capability.normalized_config.get("import_id")
    imported = db.get(CapabilityImport, import_id) if isinstance(import_id, str) else None
    if imported is None:
        return None
    for position, entry in enumerate(imported.preview_json.get("capabilities", [])):
        if entry.get("capability_key") == capability.capability_key:
            return f"{imported.id}:{position}"
    return None


def _resolve_capability(db: Session, capability_id: str) -> tuple[CapabilityImport, dict[str, Any]]:
    import_id, separator, raw_position = capability_id.rpartition(":")
    if not separator or not raw_position.isdigit():
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability reference is invalid", 422)
    imported = db.get(CapabilityImport, import_id)
    if imported is None or imported.state != "COMMITTED":
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 422)
    raw_entries: object = imported.preview_json.get("capabilities", [])
    entries = cast(list[object], raw_entries) if isinstance(raw_entries, list) else []
    position = int(raw_position)
    if position >= len(entries) or not isinstance(entries[position], dict):
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 422)
    entry = cast(dict[str, Any], entries[position])
    if entry.get("deleted_at"):
        raise DomainError("CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 422)
    raw_normalized: object = entry.get("normalized_config", {})
    normalized = cast(dict[str, Any], raw_normalized) if isinstance(raw_normalized, dict) else {}
    if normalized.get("dependencies"):
        if normalized.get("dependency_build_state") != "READY":
            raise DomainError(
                "CAPABILITY_DEPENDENCIES_NOT_READY",
                "Capability dependencies are not ready",
                409,
                {"capability_id": capability_id},
            )
    return imported, entry


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


def list_assets(
    db: Session, directory_id: str | None = None, query: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(NodeAsset).where(NodeAsset.deleted_at.is_(None))
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
    if not item or item.deleted_at:
        raise not_found("node_asset", asset_id)
    return item


def _validate_executor(db: Session, payload: NodeAssetWrite) -> None:
    executor = payload.executor
    if not executor.model_provider_id:
        if executor.model_name:
            raise DomainError("INVALID_COMMAND", "model_name requires model_provider_id", 400)
        return
    provider = db.get(ModelProvider, executor.model_provider_id)
    if not provider:
        raise not_found("model_provider", executor.model_provider_id)
    if executor.model_name:
        model = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider.id,
                ProviderModel.model_name == executor.model_name,
                ProviderModel.enabled.is_(True),
            )
        )
        if not model:
            raise DomainError(
                "INVALID_COMMAND",
                "node executor must reference an enabled provider model",
                400,
                {"model_name": executor.model_name},
            )
    else:
        default = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider.id,
                ProviderModel.enabled.is_(True),
                ProviderModel.is_default.is_(True),
            )
        )
        if not default:
            raise DomainError(
                "INVALID_COMMAND",
                "node executor provider requires an enabled default model",
                400,
            )


def _replace_children(db: Session, item: NodeAsset, payload: NodeAssetWrite) -> None:
    _validate_executor(db, payload)
    db.execute(delete(NodeIOField).where(NodeIOField.node_asset_id == item.id))
    db.execute(delete(NodeCapabilityRef).where(NodeCapabilityRef.node_asset_id == item.id))
    db.execute(delete(NodeExecutorConfig).where(NodeExecutorConfig.node_asset_id == item.id))
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
    db.add(NodeExecutorConfig(node_asset_id=item.id, **payload.executor.model_dump()))
    for position, capability in enumerate(payload.capabilities):
        capability_id = capability.capability_id
        if capability_id is not None:
            imported, canonical = _resolve_capability(db, capability_id)
            canonical_key = str(canonical.get("capability_key") or "")
            if (
                capability.capability_type is not None
                and capability.capability_type != imported.capability_type
            ) or (
                capability.capability_key is not None and capability.capability_key != canonical_key
            ):
                raise DomainError(
                    "CAPABILITY_IMPORT_INVALID",
                    "Capability reference does not match the published version",
                    422,
                )
        else:
            legacy_config = capability.normalized_config or {}
            import_id = legacy_config.get("import_id")
            imported = db.get(CapabilityImport, import_id) if isinstance(import_id, str) else None
            if not imported or imported.state != "COMMITTED":
                raise DomainError(
                    "CAPABILITY_IMPORT_REQUIRED",
                    "Node capabilities must reference a published capability",
                    422,
                    {"capability_key": capability.capability_key},
                )
            canonical = next(
                (
                    entry
                    for entry in imported.preview_json.get("capabilities", [])
                    if entry.get("capability_key") == capability.capability_key
                ),
                None,
            )
            if canonical is None or imported.capability_type != capability.capability_type:
                raise DomainError(
                    "CAPABILITY_IMPORT_INVALID", "Capability is not present in import", 422
                )
            canonical_position = imported.preview_json.get("capabilities", []).index(canonical)
            capability_id = f"{imported.id}:{canonical_position}"
        canonical_key = str(canonical.get("capability_key") or "")
        if not canonical_key:
            raise DomainError(
                "CAPABILITY_REFERENCE_INVALID", "Capability version is unavailable", 422
            )
        normalized = {
            **canonical.get("normalized_config", {}),
            "capability_id": capability_id,
            "import_id": imported.id,
            "filename": imported.filename,
            "content_hash": imported.content_hash,
            "storage_key": imported.storage_key,
        }
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=position,
                capability_type=imported.capability_type,
                capability_key=canonical_key,
                normalized_config=normalized,
            )
        )


def save_asset(db: Session, payload: NodeAssetWrite, asset_id: str | None = None) -> dict[str, Any]:
    if payload.directory_id and not db.get(NodeDirectory, payload.directory_id):
        raise not_found("node_directory", payload.directory_id)
    if payload.environment_version_id:
        environment = lock_referenceable_version(db, payload.environment_version_id)
        if environment is None:
            raise DomainError(
                "ENVIRONMENT_VERSION_INVALID",
                "Node runtime environment must reference a READY immutable version",
                422,
                {"environment_version_id": payload.environment_version_id},
            )
    if asset_id:
        item = get_asset(db, asset_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "node asset was modified", expected=payload.row_version, actual=item.row_version
            )
        item.row_version += 1
        item.updated_at = datetime.now(UTC)
    else:
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
        "environment_version_id",
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
        select(NodeAsset)
        .where(NodeAsset.id.in_(ids), NodeAsset.deleted_at.is_(None))
        .order_by(NodeAsset.id)
        .with_for_update()
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
        .where(
            FlowNode.node_asset_id.in_(ids),
            FlowDefinition.deleted_at.is_(None),
        )
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
    deleted_at = datetime.now(UTC)
    blocked_ids = {item["id"] for item in blocked}
    deleted_ids: list[str] = []
    for item in items:
        if item.id in blocked_ids:
            continue
        item.deleted_at = deleted_at
        item.row_version += 1
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
