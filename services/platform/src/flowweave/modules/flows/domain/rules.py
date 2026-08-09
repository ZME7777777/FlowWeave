from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flowweave.shared.domain.errors import DomainError


def validate_flow(
    payload: Mapping[str, Any], asset_ports: Mapping[str, Mapping[str, Mapping[str, str]]]
) -> None:
    nodes = payload.get("nodes", [])
    keys = [node["instance_key"] for node in nodes]
    if len(keys) != len(set(keys)):
        raise DomainError("FLOW_GRAPH_INVALID", "Flow node instance keys must be unique", 422)
    by_key = {node["instance_key"]: node for node in nodes}
    default_entry = payload.get("default_entry_key")
    if default_entry and default_entry not in by_key:
        raise DomainError("FLOW_GRAPH_INVALID", "Default entry is not a flow node", 422)
    for node in nodes:
        positions = [(gate["stage"], gate["position"]) for gate in node.get("gates", [])]
        if len(positions) != len(set(positions)):
            raise DomainError(
                "FLOW_GRAPH_INVALID",
                "Gate positions must be unique",
                422,
                {"node": node["instance_key"]},
            )
    adjacency: dict[str, set[str]] = {key: set() for key in keys}
    for edge in payload.get("edges", []):
        source_key = edge["source_instance_key"]
        target_key = edge["target_instance_key"]
        if source_key not in by_key or target_key not in by_key:
            raise DomainError("FLOW_GRAPH_INVALID", "Edge endpoint is not in this flow", 422)
        if source_key == target_key:
            raise DomainError(
                "FLOW_GRAPH_INVALID", "Flow edge cannot connect a node to itself", 422
            )
        if target_key in adjacency[source_key]:
            raise DomainError("FLOW_GRAPH_INVALID", "Flow direction edge is duplicated", 422)
        adjacency[source_key].add(target_key)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_key: str) -> None:
        if node_key in visiting:
            raise DomainError(
                "FLOW_GRAPH_INVALID", "Flow direction graph cannot contain cycles", 422
            )
        if node_key in visited:
            return
        visiting.add(node_key)
        for target_key in adjacency[node_key]:
            visit(target_key)
        visiting.remove(node_key)
        visited.add(node_key)

    for node_key in keys:
        visit(node_key)
    mappings = payload.get("port_mappings", [])
    targets: dict[tuple[str, str], tuple[str, str]] = {}
    for mapping in mappings:
        source_key = mapping["source_instance_key"]
        target_key = mapping["target_instance_key"]
        if source_key not in by_key or target_key not in by_key:
            raise DomainError(
                "FLOW_GRAPH_INVALID", "Port mapping endpoint is not in this flow", 422
            )
        if source_key == target_key:
            raise DomainError(
                "FLOW_GRAPH_INVALID", "Port mapping cannot connect a node to itself", 422
            )
        target_identity = (target_key, mapping["target_input_key"])
        source_identity = (source_key, mapping["source_output_key"])
        if target_identity in targets:
            raise DomainError("FLOW_GRAPH_INVALID", "Target input has multiple mappings", 422)
        targets[target_identity] = source_identity
        source = asset_ports[by_key[source_key]["node_asset_id"]]["OUTPUT"]
        target = asset_ports[by_key[target_key]["node_asset_id"]]["INPUT"]
        source_type = source.get(mapping["source_output_key"])
        target_type = target.get(mapping["target_input_key"])
        if source_type is None:
            raise DomainError("FLOW_GRAPH_INVALID", "Unknown source output", 422)
        if target_type is None:
            raise DomainError("FLOW_GRAPH_INVALID", "Unknown target input", 422)
        if source_type != target_type:
            raise DomainError("FLOW_GRAPH_INVALID", "Mapped field types are incompatible", 422)
