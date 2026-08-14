from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import (
    ensure_context_policy,
    ensure_default_critic_policy,
    ensure_default_memory_policy,
    ensure_default_tool_policy,
    resolve_version,
)
from flowweave.runtime.workspace import (
    cleanup_node_workspace,
    materialize_node_workspace,
    node_workspace_relative,
)
from flowweave.shared.application.transactions import finish, register_commit_action
from flowweave.shared.domain.agent_definition import (
    normalize_agent_definition_document,
)
from flowweave.shared.domain.runtime_policy import (
    normalize_agent_profile_document,
    normalize_context_policy_document,
    normalize_critic_policy_document,
    normalize_memory_policy_document,
    validate_agent_profile_materialization,
)
from flowweave.shared.domain.tool_policy import (
    normalize_tool_entries,
    normalize_tool_policy_document,
)
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import (
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
        .where(
            NodeCapabilityRef.node_asset_id == item.id,
            NodeCapabilityRef.capability_type.in_(
                (
                    "SKILL",
                    "PLUGIN",
                    "MCP",
                    "HOOK",
                    "TOOL_POLICY",
                    "AGENT_DEFINITION",
                    "CONTEXT_POLICY",
                    "MEMORY_POLICY",
                    "CRITIC_POLICY",
                    "AGENT_PROFILE",
                )
            ),
        )
        .order_by(NodeCapabilityRef.position)
    ).all()

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
            "confirmation_policy": executor.confirmation_policy,
            "condenser": executor.condenser_config_json or {"kind": "NO_OP"},
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
    del db
    return capability.capability_version_id


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


def _validate_executor(db: Session, payload: NodeAssetWrite) -> dict[str, Any]:
    executor = payload.executor
    condenser = executor.condenser
    if not executor.model_provider_id:
        if executor.model_name:
            raise DomainError("INVALID_COMMAND", "model_name requires model_provider_id", 400)
        if condenser.kind == "LLM_SUMMARIZING" and not condenser.model_provider_id:
            raise DomainError(
                "INVALID_COMMAND",
                "LLM summarizing condenser requires a model provider",
                400,
            )
    else:
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
    condenser_provider_id = condenser.model_provider_id or executor.model_provider_id
    if condenser.kind == "LLM_SUMMARIZING":
        if not condenser_provider_id:
            raise DomainError(
                "INVALID_COMMAND",
                "LLM summarizing condenser requires a model provider",
                400,
            )
        condenser_provider = db.get(ModelProvider, condenser_provider_id)
        if not condenser_provider:
            raise not_found("model_provider", condenser_provider_id)
        query = select(ProviderModel).where(
            ProviderModel.provider_id == condenser_provider_id,
            ProviderModel.enabled.is_(True),
        )
        query = (
            query.where(ProviderModel.model_name == condenser.model_name)
            if condenser.model_name
            else query.where(ProviderModel.is_default.is_(True))
        )
        condenser_model = db.scalar(query)
        if condenser_model is None:
            raise DomainError(
                "INVALID_COMMAND",
                "condenser must reference an enabled provider model",
                400,
                {"model_name": condenser.model_name},
            )
        # Freeze inherited/default selections before this node enters a Run
        # Snapshot. Runtime credentials are resolved later and never enter JSON.
        return {
            **condenser.model_dump(mode="json"),
            "model_provider_id": condenser_provider_id,
            "model_name": condenser_model.model_name,
        }
    return condenser.model_dump(mode="json")


def _replace_children(db: Session, item: NodeAsset, payload: NodeAssetWrite) -> None:
    condenser_config = _validate_executor(db, payload)
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
    executor_data = payload.executor.model_dump(exclude={"condenser"})
    db.add(
        NodeExecutorConfig(
            node_asset_id=item.id,
            condenser_config_json=condenser_config,
            **executor_data,
        )
    )
    tool_policy_count = 0
    context_policy_count = 0
    memory_policy_count = 0
    critic_policy_count = 0
    agent_profile_count = 0
    selected_versions: dict[str, str] = {}
    profile_config: dict[str, Any] | None = None
    selected_policy_configs: dict[str, dict[str, Any]] = {}
    selected_mcp_names: set[str] = set()
    policy_tool_names: set[str] | None = None
    policy_confirmation_required: set[str] = set()
    agent_definitions: list[dict[str, Any]] = []
    agent_definition_names: set[str] = set()
    for position, capability in enumerate(payload.capabilities):
        capability_id = capability.capability_id
        if capability_id is None:
            raise DomainError(
                "CAPABILITY_VERSION_REQUIRED",
                "Node capabilities must reference an immutable version",
                422,
                {"capability_key": capability.capability_key},
            )
        published = resolve_version(db, capability_id)
        capability_type = published.package.capability_type
        canonical_key = published.package.capability_key
        if capability_type not in {
            "SKILL",
            "PLUGIN",
            "MCP",
            "HOOK",
            "TOOL_POLICY",
            "AGENT_DEFINITION",
            "CONTEXT_POLICY",
            "MEMORY_POLICY",
            "CRITIC_POLICY",
            "AGENT_PROFILE",
        }:
            raise DomainError(
                "CAPABILITY_TYPE_UNSUPPORTED",
                "Capability type cannot be bound to nodes",
                422,
            )
        if capability_type == "TOOL_POLICY":
            tool_policy_count += 1
            if tool_policy_count > 1:
                raise DomainError(
                    "TOOL_POLICY_CONFLICT",
                    "A node must reference exactly one Tool Policy",
                    422,
                )
        elif capability_type == "CONTEXT_POLICY":
            context_policy_count += 1
            if context_policy_count > 1:
                raise DomainError(
                    "CONTEXT_POLICY_CONFLICT",
                    "A node must reference exactly one Context Policy",
                    422,
                )
        elif capability_type == "MEMORY_POLICY":
            memory_policy_count += 1
            if memory_policy_count > 1:
                raise DomainError(
                    "MEMORY_POLICY_CONFLICT",
                    "A node must reference exactly one Memory Policy",
                    422,
                )
        elif capability_type == "CRITIC_POLICY":
            critic_policy_count += 1
            if critic_policy_count > 1:
                raise DomainError(
                    "CRITIC_POLICY_CONFLICT",
                    "A node must reference exactly one Critic Policy",
                    422,
                )
        elif capability_type == "AGENT_PROFILE":
            agent_profile_count += 1
            if agent_profile_count > 1:
                raise DomainError(
                    "AGENT_PROFILE_CONFLICT",
                    "A node can materialize at most one Agent Profile",
                    422,
                )
        elif capability_type == "MCP":
            selected_mcp_names.add(canonical_key)
        if (
            capability.capability_type is not None and capability.capability_type != capability_type
        ) or (capability.capability_key is not None and capability.capability_key != canonical_key):
            raise DomainError(
                "CAPABILITY_REFERENCE_INVALID",
                "Capability reference does not match the published version",
                422,
            )
        normalized = published.runtime_config()
        if capability_type == "TOOL_POLICY":
            try:
                policy_key, policy_config = normalize_tool_policy_document(
                    published.version.normalized_config_json,
                    fallback_key=canonical_key,
                )
            except ValueError as exc:
                raise DomainError(
                    "TOOL_POLICY_INVALID",
                    "Published Tool Policy is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if policy_key != canonical_key:
                raise DomainError(
                    "TOOL_POLICY_INVALID",
                    "Tool Policy identity does not match its Package",
                    422,
                )
            if published.version.normalized_config_json != policy_config:
                raise DomainError(
                    "TOOL_POLICY_UPGRADE_REQUIRED",
                    "Tool Policy must be republished against the governed OpenHands catalog",
                    409,
                    {"capability_id": published.version.id},
                )
            policy_tool_names = {
                str(entry["name"]) for entry in cast(list[dict[str, Any]], policy_config["tools"])
            }
            policy_confirmation_required = set(
                cast(list[str], policy_config["confirmation_required_tools"])
            )
            selected_policy_configs["TOOL_POLICY"] = policy_config
        elif capability_type == "AGENT_DEFINITION":
            try:
                definition_name, definition = normalize_agent_definition_document(
                    published.version.normalized_config_json,
                    fallback_key=canonical_key,
                )
            except ValueError as exc:
                raise DomainError(
                    "AGENT_DEFINITION_INVALID",
                    "Published Agent Definition is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if definition_name != canonical_key:
                raise DomainError(
                    "AGENT_DEFINITION_INVALID",
                    "Agent Definition identity does not match its Package",
                    422,
                )
            if definition_name in agent_definition_names:
                raise DomainError(
                    "AGENT_DEFINITION_CONFLICT",
                    "Agent Definition names must be unique within a node",
                    422,
                    {"name": definition_name},
                )
            agent_definition_names.add(definition_name)
            agent_definitions.append(definition)
        elif capability_type == "CONTEXT_POLICY":
            try:
                policy_key, _policy_config = normalize_context_policy_document(
                    published.version.normalized_config_json,
                    fallback_key=canonical_key,
                )
            except ValueError as exc:
                raise DomainError(
                    "CONTEXT_POLICY_INVALID",
                    "Published Context Policy is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if policy_key != canonical_key:
                raise DomainError(
                    "CONTEXT_POLICY_INVALID",
                    "Context Policy identity does not match its Package",
                    422,
                )
            selected_policy_configs["CONTEXT_POLICY"] = _policy_config
        elif capability_type == "MEMORY_POLICY":
            try:
                policy_key, policy_config = normalize_memory_policy_document(
                    published.version.normalized_config_json, fallback_key=canonical_key
                )
            except ValueError as exc:
                raise DomainError(
                    "MEMORY_POLICY_INVALID",
                    "Published Memory Policy is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if policy_key != canonical_key:
                raise DomainError(
                    "MEMORY_POLICY_INVALID",
                    "Memory Policy identity does not match its Package",
                    422,
                )
        elif capability_type == "CRITIC_POLICY":
            try:
                policy_key, _policy_config = normalize_critic_policy_document(
                    published.version.normalized_config_json, fallback_key=canonical_key
                )
            except ValueError as exc:
                raise DomainError(
                    "CRITIC_POLICY_INVALID",
                    "Published Critic Policy is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if policy_key != canonical_key:
                raise DomainError(
                    "CRITIC_POLICY_INVALID",
                    "Critic Policy identity does not match its Package",
                    422,
                )
            selected_policy_configs["CRITIC_POLICY"] = _policy_config
        elif capability_type == "AGENT_PROFILE":
            try:
                profile_key, profile_config = normalize_agent_profile_document(
                    published.version.normalized_config_json, fallback_key=canonical_key
                )
            except ValueError as exc:
                raise DomainError(
                    "AGENT_PROFILE_INVALID",
                    "Published Agent Profile is invalid",
                    422,
                    {"reason": str(exc)},
                ) from exc
            if profile_key != canonical_key:
                raise DomainError(
                    "AGENT_PROFILE_INVALID",
                    "Agent Profile identity does not match its Package",
                    422,
                )
        if normalized.get("dependencies") and normalized.get("dependency_build_state") != "READY":
            raise DomainError(
                "CAPABILITY_DEPENDENCIES_NOT_READY",
                "Capability dependencies are not ready",
                409,
                {"capability_id": capability_id},
            )
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=position,
                capability_type=capability_type,
                capability_key=canonical_key,
                capability_version_id=published.version.id,
                normalized_config=normalized,
            )
        )
        selected_versions[capability_type] = published.version.id
    if tool_policy_count == 0:
        policy = ensure_default_tool_policy(db)
        selected_versions["TOOL_POLICY"] = policy.version.id
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=len(payload.capabilities),
                capability_type="TOOL_POLICY",
                capability_key=policy.package.capability_key,
                capability_version_id=policy.version.id,
                normalized_config=policy.runtime_config(),
            )
        )
        policy_tool_names = {
            str(entry["name"])
            for entry in normalize_tool_entries(policy.runtime_config().get("tools"))
        }
        policy_confirmation_required = set(
            cast(list[str], policy.runtime_config()["confirmation_required_tools"])
        )
        selected_policy_configs["TOOL_POLICY"] = normalize_tool_policy_document(
            policy.version.normalized_config_json,
            fallback_key=policy.package.capability_key,
        )[1]
    if context_policy_count == 0:
        context_policy = ensure_context_policy(db)
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=len(payload.capabilities) + (1 if tool_policy_count == 0 else 0),
                capability_type="CONTEXT_POLICY",
                capability_key=context_policy.package.capability_key,
                capability_version_id=context_policy.version.id,
                normalized_config=context_policy.runtime_config(),
            )
        )
        selected_versions["CONTEXT_POLICY"] = context_policy.version.id
        selected_policy_configs["CONTEXT_POLICY"] = normalize_context_policy_document(
            context_policy.version.normalized_config_json,
            fallback_key=context_policy.package.capability_key,
        )[1]
    if memory_policy_count == 0:
        memory_policy = ensure_default_memory_policy(db)
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=len(payload.capabilities) + 2,
                capability_type="MEMORY_POLICY",
                capability_key=memory_policy.package.capability_key,
                capability_version_id=memory_policy.version.id,
                normalized_config=memory_policy.runtime_config(),
            )
        )
        selected_versions["MEMORY_POLICY"] = memory_policy.version.id
    if critic_policy_count == 0:
        critic_policy = ensure_default_critic_policy(db)
        db.add(
            NodeCapabilityRef(
                node_asset_id=item.id,
                position=len(payload.capabilities) + 3,
                capability_type="CRITIC_POLICY",
                capability_key=critic_policy.package.capability_key,
                capability_version_id=critic_policy.version.id,
                normalized_config=critic_policy.runtime_config(),
            )
        )
        selected_versions["CRITIC_POLICY"] = critic_policy.version.id
        selected_policy_configs["CRITIC_POLICY"] = normalize_critic_policy_document(
            critic_policy.version.normalized_config_json,
            fallback_key=critic_policy.package.capability_key,
        )[1]
    if profile_config is not None:
        references = {
            "tool_policy_version_id": "TOOL_POLICY",
            "context_policy_version_id": "CONTEXT_POLICY",
            "memory_policy_version_id": "MEMORY_POLICY",
            "critic_policy_version_id": "CRITIC_POLICY",
        }
        mismatches = {
            field: {"profile": profile_config.get(field), "node": selected_versions.get(kind)}
            for field, kind in references.items()
            if profile_config.get(field) is not None
            and profile_config.get(field) != selected_versions.get(kind)
        }
        if mismatches:
            raise DomainError(
                "AGENT_PROFILE_REFERENCE_MISMATCH",
                "Agent Profile references must match the immutable policies bound to the node",
                422,
                {"mismatches": mismatches},
            )
        if (
            profile_config["confirmation_policy"] != payload.executor.confirmation_policy
            or profile_config["max_iterations"] != payload.executor.max_iterations
        ):
            raise DomainError(
                "AGENT_PROFILE_EXECUTOR_MISMATCH",
                "Agent Profile confirmation and iteration settings must be "
                "materialized on the node executor",
                422,
            )
        try:
            validate_agent_profile_materialization(
                profile_config,
                tool_policy=selected_policy_configs["TOOL_POLICY"],
                context_policy=selected_policy_configs["CONTEXT_POLICY"],
                critic_policy=selected_policy_configs["CRITIC_POLICY"],
                mcp_server_names=selected_mcp_names,
                agent_definitions_enabled=bool(agent_definitions),
            )
        except ValueError as exc:
            raise DomainError(
                "AGENT_PROFILE_MATERIALIZATION_MISMATCH",
                "Agent Profile fields must match the immutable node capabilities",
                422,
                {"reason": str(exc)},
            ) from exc
    if policy_confirmation_required and payload.executor.confirmation_policy != "ALWAYS":
        raise DomainError(
            "TOOL_POLICY_CONFIRMATION_REQUIRED",
            "Tool Policy requires native confirmation for mutating or control tools",
            422,
            {"tools": sorted(policy_confirmation_required)},
        )
    if agent_definitions:
        if policy_tool_names is None or "task_tool_set" not in policy_tool_names:
            raise DomainError(
                "AGENT_DEFINITION_TASK_TOOL_REQUIRED",
                "Nodes with Agent Definitions must explicitly allow task_tool_set",
                422,
            )
        for definition in agent_definitions:
            missing = sorted(set(cast(list[str], definition["tools"])) - policy_tool_names)
            if missing:
                raise DomainError(
                    "AGENT_DEFINITION_TOOL_POLICY_MISMATCH",
                    "Agent Definition tools must be a subset of the node Tool Policy",
                    422,
                    {"name": definition["name"], "tools": missing},
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
