"""Stable public facade for the flows module."""

from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.flows.application.service import flow_dict, get_flow
from flowweave.shared.models import FlowDefinition


def load_flow(db: Session, flow_id: str) -> FlowDefinition:
    """Load one active flow definition."""

    return get_flow(db, flow_id)


def describe_flow(db: Session, flow: FlowDefinition) -> dict[str, Any]:
    """Return the stable flow read model used by snapshots."""

    return flow_dict(db, flow)


__all__ = ("describe_flow", "load_flow")
