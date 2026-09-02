from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class FlowDefinition(Base):
    __tablename__ = "flow_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    default_entry_key: Mapped[str | None] = mapped_column(String(100))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class FlowNode(Base):
    __tablename__ = "flow_nodes"
    __table_args__ = (UniqueConstraint("flow_id", "instance_key", name="uq_flow_instance_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_id: Mapped[str] = mapped_column(String(36), index=True)
    instance_key: Mapped[str] = mapped_column(String(100))
    node_asset_id: Mapped[str] = mapped_column(String(36))
    alias: Mapped[str | None] = mapped_column(String(200))
    position_x: Mapped[int] = mapped_column(Integer, default=0)
    position_y: Mapped[int] = mapped_column(Integer, default=0)
    config_override: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FlowEdge(Base):
    __tablename__ = "flow_edges"
    __table_args__ = (
        UniqueConstraint(
            "flow_id",
            "source_flow_node_id",
            "target_flow_node_id",
            "position",
            name="uq_flow_edge_position",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_id: Mapped[str] = mapped_column(String(36), index=True)
    source_flow_node_id: Mapped[str] = mapped_column(String(36))
    target_flow_node_id: Mapped[str] = mapped_column(String(36))
    position: Mapped[int] = mapped_column(Integer, default=0)


class FlowPortMapping(Base):
    __tablename__ = "flow_port_mappings"
    __table_args__ = (
        UniqueConstraint(
            "flow_id",
            "target_flow_node_id",
            "target_input_key",
            name="uq_flow_target_input_mapping",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_id: Mapped[str] = mapped_column(String(36), index=True)
    source_flow_node_id: Mapped[str] = mapped_column(String(36))
    source_output_key: Mapped[str] = mapped_column(String(100))
    target_flow_node_id: Mapped[str] = mapped_column(String(36))
    target_input_key: Mapped[str] = mapped_column(String(100))


class GatePolicy(Base):
    __tablename__ = "gate_policies"
    __table_args__ = (
        UniqueConstraint("flow_node_id", "stage", "position", name="uq_gate_stage_position"),
        CheckConstraint("timeout_seconds > 0", name="ck_gate_timeout_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_node_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(10))
    position: Mapped[int] = mapped_column(Integer)
    gate_type: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))


__all__ = (
    "FlowDefinition",
    "FlowNode",
    "FlowEdge",
    "FlowPortMapping",
    "GatePolicy",
)
