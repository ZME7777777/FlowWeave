from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, cast

from flowweave.runtime.contract import normalize_runtime_contract
from flowweave.shared.domain.capability_digest import (
    capability_version_digest,
    normalized_capability_config,
)
from flowweave.shared.domain.runtime_policy import (
    normalize_agent_profile_document,
    normalize_context_policy_document,
    normalize_critic_policy_document,
    normalize_memory_policy_document,
    validate_agent_profile_materialization,
)
from flowweave.shared.domain.tool_policy import (
    OPENHANDS_VERSION,
    normalize_tool_policy_document,
)
from flowweave.shared.errors import DomainError


def _runtime_policy(
    agent_spec: dict[str, Any],
    *,
    field: str,
    capability_type: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_entry = agent_spec.get(field)
    if not isinstance(raw_entry, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            f"Snapshot Runtime {capability_type} is missing",
            409,
        )
    entry = cast(dict[str, Any], raw_entry)
    raw_config = entry.get("runtime_config")
    config = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
    version_id = str(entry.get("capability_version_id") or "")
    expected_digest = capability_version_digest(
        str(entry.get("capability_type") or ""),
        str(entry.get("capability_key") or ""),
        str(entry.get("content_hash") or ""),
        normalized_capability_config(config),
    )
    if (
        entry.get("capability_type") != capability_type
        or len(version_id) != 36
        or not isinstance(raw_config, dict)
        or config.get("capability_version_id") != version_id
        or config.get("digest") != entry.get("digest")
        or config.get("content_hash") != entry.get("content_hash")
        or entry.get("digest") != expected_digest
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            f"Snapshot {capability_type} identity drifted",
            409,
            {"capability_version_id": version_id},
        )
    return entry, normalized_capability_config(config)


def runtime_manifest_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _definition_node(
    definition: dict[str, Any], instance_key: str, snapshot_id: str
) -> dict[str, Any]:
    raw_nodes: object = definition.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DomainError(
            "SNAPSHOT_INVALID",
            "Snapshot nodes are invalid",
            409,
            {"snapshot_id": snapshot_id},
        )
    for raw_node in cast(list[object], raw_nodes):
        if isinstance(raw_node, dict):
            node = cast(dict[str, Any], raw_node)
            if node.get("instance_key") == instance_key:
                return copy.deepcopy(node)
    raise DomainError(
        "SNAPSHOT_MANIFEST_INVALID",
        "Snapshot Runtime Manifest has no selected node",
        409,
        {"snapshot_id": snapshot_id, "instance_key": instance_key},
    )


def runtime_node(
    *,
    definition: dict[str, Any],
    manifest: dict[str, Any],
    expected_hash: str,
    snapshot_id: str,
    instance_key: str,
) -> dict[str, Any]:
    """Project one executable node solely from a frozen Runtime Manifest."""

    if runtime_manifest_hash(manifest) != expected_hash:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest hash does not match",
            409,
            {"snapshot_id": snapshot_id},
        )
    raw_nodes: object = manifest.get("nodes")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("openhands_version") != OPENHANDS_VERSION
        or not isinstance(raw_nodes, dict)
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest schema is invalid",
            409,
            {"snapshot_id": snapshot_id},
        )
    raw_manifest_node = cast(dict[object, object], raw_nodes).get(instance_key)
    if not isinstance(raw_manifest_node, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest has no selected node",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    manifest_node = cast(dict[object, object], raw_manifest_node)
    raw_capabilities: object = manifest_node.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest capabilities are invalid",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )

    capabilities: list[dict[str, Any]] = []
    for raw_capability in cast(list[object], raw_capabilities):
        if not isinstance(raw_capability, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Runtime Manifest capability is invalid",
                409,
            )
        capability = cast(dict[str, Any], raw_capability)
        raw_config: object = capability.get("runtime_config")
        if not isinstance(raw_config, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Runtime Manifest capability config is invalid",
                409,
            )
        config = copy.deepcopy(cast(dict[str, Any], raw_config))
        version_id = str(capability.get("capability_version_id") or "")
        capability_type = str(capability.get("capability_type") or "")
        capability_key = str(capability.get("capability_key") or "")
        content_hash = str(capability.get("content_hash") or "")
        expected_digest = capability_version_digest(
            capability_type,
            capability_key,
            content_hash,
            normalized_capability_config(config),
        )
        if (
            config.get("capability_version_id") != version_id
            or config.get("digest") != capability.get("digest")
            or config.get("content_hash") != capability.get("content_hash")
            or capability.get("digest") != expected_digest
        ):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Runtime Manifest capability identity drifted",
                409,
                {"capability_version_id": version_id},
            )
        capabilities.append(
            {
                "capability_id": version_id,
                "capability_type": capability_type,
                "capability_key": capability_key,
                "normalized_config": config,
            }
        )

    raw_agent_spec = manifest_node.get("agent_spec")
    if not isinstance(raw_agent_spec, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Agent Spec is missing",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    agent_spec = copy.deepcopy(cast(dict[str, Any], raw_agent_spec))
    raw_tool_policy = agent_spec.get("tool_policy")
    if (
        agent_spec.get("schema_version") != 1
        or agent_spec.get("agent_kind") != "OPENHANDS"
        or agent_spec.get("openhands_version") != OPENHANDS_VERSION
        or not isinstance(raw_tool_policy, dict)
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Agent Spec schema is invalid",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    raw_agent_definitions = agent_spec.get("agent_definitions", [])
    if not isinstance(raw_agent_definitions, list):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Agent Definitions are invalid",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    definition_names: set[str] = set()
    for raw_definition in cast(list[object], raw_agent_definitions):
        if not isinstance(raw_definition, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Runtime Agent Definition is invalid",
                409,
            )
        definition_entry = cast(dict[str, Any], raw_definition)
        raw_config = definition_entry.get("runtime_config")
        version_id = str(definition_entry.get("capability_version_id") or "")
        config = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
        expected_digest = capability_version_digest(
            str(definition_entry.get("capability_type") or ""),
            str(definition_entry.get("capability_key") or ""),
            str(definition_entry.get("content_hash") or ""),
            normalized_capability_config(config),
        )
        name = str(config.get("name") or "")
        if (
            definition_entry.get("capability_type") != "AGENT_DEFINITION"
            or len(version_id) != 36
            or not isinstance(raw_config, dict)
            or config.get("capability_version_id") != version_id
            or config.get("digest") != definition_entry.get("digest")
            or config.get("content_hash") != definition_entry.get("content_hash")
            or definition_entry.get("digest") != expected_digest
            or not name
            or name in definition_names
        ):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Agent Definition identity drifted",
                409,
                {"capability_version_id": version_id},
            )
        definition_names.add(name)
    raw_context_policy = agent_spec.get("context_policy")
    if not isinstance(raw_context_policy, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Context Policy is missing",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    context_policy = cast(dict[str, Any], raw_context_policy)
    raw_context_config = context_policy.get("runtime_config")
    context_version_id = str(context_policy.get("capability_version_id") or "")
    context_config = (
        cast(dict[str, Any], raw_context_config) if isinstance(raw_context_config, dict) else {}
    )
    context_digest = capability_version_digest(
        str(context_policy.get("capability_type") or ""),
        str(context_policy.get("capability_key") or ""),
        str(context_policy.get("content_hash") or ""),
        normalized_capability_config(context_config),
    )
    try:
        context_name, _ = normalize_context_policy_document(
            normalized_capability_config(context_config),
            fallback_key=str(context_policy.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Context Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if (
        context_policy.get("capability_type") != "CONTEXT_POLICY"
        or len(context_version_id) != 36
        or not isinstance(raw_context_config, dict)
        or context_config.get("capability_version_id") != context_version_id
        or context_config.get("digest") != context_policy.get("digest")
        or context_config.get("content_hash") != context_policy.get("content_hash")
        or context_policy.get("digest") != context_digest
        or context_name != context_policy.get("capability_key")
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Context Policy identity drifted",
            409,
            {"capability_version_id": context_version_id},
        )
    memory_policy, memory_config = _runtime_policy(
        agent_spec, field="memory_policy", capability_type="MEMORY_POLICY"
    )
    critic_policy, critic_config = _runtime_policy(
        agent_spec, field="critic_policy", capability_type="CRITIC_POLICY"
    )
    try:
        memory_name, _ = normalize_memory_policy_document(
            memory_config, fallback_key=str(memory_policy.get("capability_key") or "")
        )
        critic_name, _ = normalize_critic_policy_document(
            critic_config, fallback_key=str(critic_policy.get("capability_key") or "")
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Memory or Critic Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if memory_name != memory_policy.get("capability_key") or critic_name != critic_policy.get(
        "capability_key"
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime policy identity drifted",
            409,
        )
    raw_profile = agent_spec.get("agent_profile")
    if raw_profile is not None:
        profile, profile_config = _runtime_policy(
            agent_spec, field="agent_profile", capability_type="AGENT_PROFILE"
        )
        try:
            profile_name, normalized_profile = normalize_agent_profile_document(
                profile_config, fallback_key=str(profile.get("capability_key") or "")
            )
        except ValueError as exc:
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Agent Profile is invalid",
                409,
                {"reason": str(exc)},
            ) from exc
        expected_refs = {
            "tool_policy_version_id": str(
                cast(dict[str, Any], raw_tool_policy).get("capability_version_id") or ""
            ),
            "context_policy_version_id": context_version_id,
            "memory_policy_version_id": str(memory_policy.get("capability_version_id") or ""),
            "critic_policy_version_id": str(critic_policy.get("capability_version_id") or ""),
        }
        raw_budgets = agent_spec.get("budgets")
        profile_drifted = (
            profile_name != profile.get("capability_key")
            or any(
                normalized_profile.get(field) != version_id
                for field, version_id in expected_refs.items()
            )
            or not isinstance(raw_budgets, dict)
            or normalized_profile.get("max_iterations")
            != cast(dict[str, Any], raw_budgets).get("max_iterations")
        )
        if profile_drifted:
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Agent Profile drifted from its materialized Agent Spec",
                409,
            )
        try:
            tool_policy_entry = cast(dict[str, Any], raw_tool_policy)
            validate_agent_profile_materialization(
                normalized_profile,
                tool_policy=normalize_tool_policy_document(
                    normalized_capability_config(
                        cast(dict[str, Any], raw_tool_policy["runtime_config"])
                    ),
                    fallback_key=str(tool_policy_entry.get("capability_key") or ""),
                )[1],
                context_policy=context_config,
                critic_policy=critic_config,
                mcp_server_names={
                    str(item.get("capability_key") or "")
                    for item in capabilities
                    if item.get("capability_type") == "MCP"
                },
                agent_definitions_enabled=bool(agent_spec.get("agent_definitions")),
            )
        except ValueError as exc:
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Agent Profile materialization drifted",
                409,
                {"reason": str(exc)},
            ) from exc
    raw_plugins = agent_spec.get("plugins", [])
    if not isinstance(raw_plugins, list):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Plugins are invalid",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    plugin_names: set[str] = set()
    for raw_plugin in cast(list[object], raw_plugins):
        if not isinstance(raw_plugin, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID", "Snapshot Runtime Plugin is invalid", 409
            )
        plugin = cast(dict[str, Any], raw_plugin)
        raw_config = plugin.get("runtime_config")
        version_id = str(plugin.get("capability_version_id") or "")
        config = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
        expected_digest = capability_version_digest(
            str(plugin.get("capability_type") or ""),
            str(plugin.get("capability_key") or ""),
            str(plugin.get("content_hash") or ""),
            normalized_capability_config(config),
        )
        name = str(plugin.get("capability_key") or "")
        if (
            plugin.get("capability_type") != "PLUGIN"
            or len(version_id) != 36
            or not isinstance(raw_config, dict)
            or config.get("capability_version_id") != version_id
            or config.get("digest") != plugin.get("digest")
            or config.get("content_hash") != plugin.get("content_hash")
            or plugin.get("digest") != expected_digest
            or config.get("package_format") != "openhands-plugin-v1"
            or not isinstance(config.get("file_hashes"), dict)
            or not name
            or name in plugin_names
        ):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Snapshot Plugin identity drifted",
                409,
                {"capability_version_id": version_id},
            )
        plugin_names.add(name)
        capabilities.append(
            {
                "capability_id": version_id,
                "capability_type": "PLUGIN",
                "capability_key": name,
                "normalized_config": copy.deepcopy(config),
            }
        )
    tool_policy = cast(dict[str, Any], raw_tool_policy)
    raw_policy_config = tool_policy.get("runtime_config")
    policy_version_id = str(tool_policy.get("capability_version_id") or "")
    policy_config = (
        cast(dict[str, Any], raw_policy_config) if isinstance(raw_policy_config, dict) else {}
    )
    policy_digest = capability_version_digest(
        str(tool_policy.get("capability_type") or ""),
        str(tool_policy.get("capability_key") or ""),
        str(tool_policy.get("content_hash") or ""),
        normalized_capability_config(policy_config),
    )
    if (
        tool_policy.get("capability_type") != "TOOL_POLICY"
        or len(policy_version_id) != 36
        or not isinstance(raw_policy_config, dict)
        or policy_config.get("capability_version_id") != policy_version_id
        or policy_config.get("digest") != tool_policy.get("digest")
        or policy_config.get("content_hash") != tool_policy.get("content_hash")
        or tool_policy.get("digest") != policy_digest
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Tool Policy identity drifted",
            409,
            {"capability_version_id": policy_version_id},
        )
    try:
        policy_key, normalized_policy = normalize_tool_policy_document(
            normalized_capability_config(policy_config),
            fallback_key=str(tool_policy.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Tool Policy config is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if policy_key != tool_policy.get("capability_key"):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Tool Policy identity drifted",
            409,
        )
    if normalized_capability_config(policy_config) != normalized_policy:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Tool Policy predates the governed catalog and must be republished",
            409,
            {"capability_version_id": policy_version_id},
        )
    policy_tools = cast(list[dict[str, Any]], normalized_policy["tools"])
    required_tools = tuple(str(item["name"]) for item in policy_tools)
    try:
        normalize_runtime_contract(
            agent_spec.get("runtime_contract"), required_tools=required_tools
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime contract is invalid",
            409,
            {"reason": str(exc)},
        ) from exc

    node = _definition_node(definition, instance_key, snapshot_id)
    raw_asset = node.get("asset")
    if not isinstance(raw_asset, dict):
        raise DomainError("SNAPSHOT_INVALID", "Snapshot node asset is invalid", 409)
    asset = cast(dict[str, Any], raw_asset)
    asset["capabilities"] = capabilities
    node["runtime_agent_spec"] = agent_spec
    node["runtime_snapshot_id"] = snapshot_id
    return node
