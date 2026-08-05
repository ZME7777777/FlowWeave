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
    for edge in payload.get("edges", []):
        source_key = edge["source_instance_key"]
        target_key = edge["target_instance_key"]
        if source_key not in by_key or target_key not in by_key:
            raise DomainError("FLOW_GRAPH_INVALID", "Edge endpoint is not in this flow", 422)
        source = asset_ports[by_key[source_key]["node_asset_id"]]["OUTPUT"]
        target = asset_ports[by_key[target_key]["node_asset_id"]]["INPUT"]
        for mapping in edge.get("mappings", []):
            source_type = source.get(mapping["source_output_key"])
            target_type = target.get(mapping["target_input_key"])
            if source_type is None:
                raise DomainError("FLOW_GRAPH_INVALID", "Unknown source output", 422)
            if target_type is None:
                raise DomainError("FLOW_GRAPH_INVALID", "Unknown target input", 422)
            if source_type != target_type:
                raise DomainError("FLOW_GRAPH_INVALID", "Mapped field types are incompatible", 422)
