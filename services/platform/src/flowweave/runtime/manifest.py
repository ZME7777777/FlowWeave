"""Frozen FlowRun manifest projection.

Agent configuration is intentionally absent from this manifest. A Flow node
only freezes Flow identity; every conversation receives the same Runtime Agent
spec when it is started.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, cast

from flowweave.shared.domain.openhands import OPENHANDS_VERSION
from flowweave.shared.errors import DomainError


def runtime_manifest_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _definition_node(
    definition: dict[str, Any], instance_key: str, snapshot_id: str
) -> dict[str, Any]:
    raw_nodes = definition.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DomainError(
            "SNAPSHOT_INVALID", "Snapshot nodes are invalid", 409, {"snapshot_id": snapshot_id}
        )
    for raw_node in cast(list[object], raw_nodes):
        if not isinstance(raw_node, dict):
            continue
        candidate = cast(dict[str, object], raw_node)
        if candidate.get("instance_key") == instance_key:
            return copy.deepcopy(cast(dict[str, Any], candidate))
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
    """Project one node from a Tool-Policy-free, immutable Runtime manifest."""

    if runtime_manifest_hash(manifest) != expected_hash:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest hash does not match",
            409,
            {"snapshot_id": snapshot_id},
        )
    manifest_view = cast(dict[str, object], manifest)
    raw_nodes = manifest_view.get("nodes")
    if (
        manifest_view.get("schema_version") != 3
        or manifest_view.get("openhands_version") != OPENHANDS_VERSION
        or not isinstance(raw_nodes, dict)
    ):
        raise DomainError(
            "SNAPSHOT_TOOL_POLICY_REQUIRES_RERUN",
            "This historical Snapshot uses the retired Agent Tool Policy and must be rerun",
            409,
            {"snapshot_id": snapshot_id},
        )
    manifest_node_value = cast(dict[str, object], raw_nodes).get(instance_key)
    if not isinstance(manifest_node_value, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot Runtime Manifest has no selected node",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    manifest_node = cast(dict[str, object], manifest_node_value)
    node = _definition_node(definition, instance_key, snapshot_id)
    asset = node.get("asset")
    expected_asset_id = manifest_node.get("node_asset_id")
    actual_asset_id: object = node.get("node_asset_id")
    if not isinstance(actual_asset_id, str) and isinstance(asset, dict):
        actual_asset_id = cast(dict[object, object], asset).get("id")
    if not isinstance(expected_asset_id, str) or expected_asset_id != actual_asset_id:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Snapshot node identity drifted",
            409,
            {"snapshot_id": snapshot_id, "instance_key": instance_key},
        )
    node["runtime_snapshot_id"] = snapshot_id
    return node
