from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.flows.domain.rules import validate_flow
from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import conflict, not_found
from flowweave.shared.models import (
    FlowDefinition,
    FlowEdge,
    FlowEdgeMapping,
    FlowNode,
    GatePolicy,
    NodeAsset,
    NodeIOField,
)
from flowweave.shared.schemas import FlowWrite


def _ports(
    db: Session, asset_ids: set[str], *, lock_assets: bool = False
) -> dict[str, dict[str, dict[str, str]]]:
    asset_query = (
        select(NodeAsset)
        .where(NodeAsset.id.in_(asset_ids), NodeAsset.deleted_at.is_(None))
        .order_by(NodeAsset.id)
    )
    if lock_assets:
        asset_query = asset_query.with_for_update()
    assets = {x.id for x in db.scalars(asset_query)}
    if assets != asset_ids:
        raise not_found("node_asset", next(iter(asset_ids - assets)))
    result: dict[str, dict[str, dict[str, str]]] = {
        x: {"INPUT": {}, "OUTPUT": {}} for x in asset_ids
    }
    for field in db.scalars(select(NodeIOField).where(NodeIOField.node_asset_id.in_(asset_ids))):
        result[field.node_asset_id][field.direction][field.field_key] = field.data_type
    return result


def get_flow(db: Session, flow_id: str) -> FlowDefinition:
    item = db.get(FlowDefinition, flow_id)
    if not item or item.deleted_at:
        raise not_found("flow", flow_id)
    return item


def flow_dict(db: Session, item: FlowDefinition) -> dict[str, Any]:
    nodes = db.scalars(
        select(FlowNode).where(FlowNode.flow_id == item.id).order_by(FlowNode.instance_key)
    ).all()
    node_ids = [x.id for x in nodes]
    gates: Sequence[GatePolicy] = (
        db.scalars(
            select(GatePolicy)
            .where(GatePolicy.flow_node_id.in_(node_ids))
            .order_by(GatePolicy.stage, GatePolicy.position)
        ).all()
        if node_ids
        else []
    )
    gate_map: dict[str, list[dict[str, Any]]] = {x: [] for x in node_ids}
    for gate in gates:
        gate_map[gate.flow_node_id].append(
            {
                "id": gate.id,
                "stage": gate.stage,
                "position": gate.position,
                "gate_type": gate.gate_type,
                "enabled": gate.enabled,
                "timeout_seconds": gate.timeout_seconds,
                "config": gate.config,
                "content_hash": gate.content_hash,
            }
        )
    edges = db.scalars(
        select(FlowEdge).where(FlowEdge.flow_id == item.id).order_by(FlowEdge.position)
    ).all()
    mappings: Sequence[FlowEdgeMapping] = (
        db.scalars(
            select(FlowEdgeMapping).where(FlowEdgeMapping.edge_id.in_([x.id for x in edges]))
        ).all()
        if edges
        else []
    )
    mapping_map: dict[str, list[dict[str, Any]]] = {x.id: [] for x in edges}
    for mapping in mappings:
        mapping_map[mapping.edge_id].append(
            {
                "source_output_key": mapping.source_output_key,
                "target_input_key": mapping.target_input_key,
            }
        )
    key_by_id = {x.id: x.instance_key for x in nodes}
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "default_entry_key": item.default_entry_key,
        "row_version": item.row_version,
        "nodes": [
            {
                "id": x.id,
                "instance_key": x.instance_key,
                "node_asset_id": x.node_asset_id,
                "alias": x.alias,
                "position_x": x.position_x,
                "position_y": x.position_y,
                "config_override": x.config_override,
                "gates": gate_map[x.id],
            }
            for x in nodes
        ],
        "edges": [
            {
                "id": x.id,
                "source_instance_key": key_by_id[x.source_flow_node_id],
                "target_instance_key": key_by_id[x.target_flow_node_id],
                "position": x.position,
                "mappings": mapping_map[x.id],
            }
            for x in edges
        ],
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def list_flows(db: Session) -> list[dict[str, Any]]:
    return [
        flow_dict(db, x)
        for x in db.scalars(
            select(FlowDefinition)
            .where(FlowDefinition.deleted_at.is_(None))
            .order_by(FlowDefinition.updated_at.desc())
        )
    ]


def validate_saved_flow(db: Session, flow_id: str) -> dict[str, Any]:
    payload = FlowWrite.model_validate(flow_dict(db, get_flow(db, flow_id)))
    validate_flow(
        payload.model_dump(),
        _ports(db, {node.node_asset_id for node in payload.nodes}),
    )
    return {"valid": True, "errors": []}


def save_flow(db: Session, payload: FlowWrite, flow_id: str | None = None) -> dict[str, Any]:
    ports = _ports(db, {x.node_asset_id for x in payload.nodes}, lock_assets=True)
    validate_flow(payload.model_dump(), ports)
    if flow_id:
        item = get_flow(db, flow_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "flow was modified", expected=payload.row_version, actual=item.row_version
            )
        item.row_version += 1
        item.updated_at = datetime.now(UTC)
        old_nodes = db.scalars(select(FlowNode.id).where(FlowNode.flow_id == item.id)).all()
        if old_nodes:
            db.execute(delete(GatePolicy).where(GatePolicy.flow_node_id.in_(old_nodes)))
        old_edges = db.scalars(select(FlowEdge.id).where(FlowEdge.flow_id == item.id)).all()
        if old_edges:
            db.execute(delete(FlowEdgeMapping).where(FlowEdgeMapping.edge_id.in_(old_edges)))
        db.execute(delete(FlowEdge).where(FlowEdge.flow_id == item.id))
        db.execute(delete(FlowNode).where(FlowNode.flow_id == item.id))
    else:
        item = FlowDefinition(
            name=payload.name,
            description=payload.description,
            default_entry_key=payload.default_entry_key,
        )
        db.add(item)
        db.flush()
    item.name = payload.name
    item.description = payload.description
    item.default_entry_key = payload.default_entry_key
    by_key: dict[str, FlowNode] = {}
    for node in payload.nodes:
        values = node.model_dump(exclude={"gates"})
        row = FlowNode(flow_id=item.id, **values)
        db.add(row)
        db.flush()
        by_key[node.instance_key] = row
        for gate in node.gates:
            content_hash = hashlib.sha256(
                json.dumps(gate.model_dump(), sort_keys=True).encode()
            ).hexdigest()
            db.add(GatePolicy(flow_node_id=row.id, content_hash=content_hash, **gate.model_dump()))
    for edge in payload.edges:
        row = FlowEdge(
            flow_id=item.id,
            source_flow_node_id=by_key[edge.source_instance_key].id,
            target_flow_node_id=by_key[edge.target_instance_key].id,
            position=edge.position,
        )
        db.add(row)
        db.flush()
        for mapping in edge.mappings:
            db.add(FlowEdgeMapping(edge_id=row.id, **mapping.model_dump()))
    finish(db)
    return flow_dict(db, item)


def delete_flow(db: Session, flow_id: str) -> None:
    item = get_flow(db, flow_id)
    item.deleted_at = datetime.now(UTC)
    item.row_version += 1
    finish(db)
