from __future__ import annotations

from sqlalchemy.orm import Session

from flowweave.shared.errors import not_found
from flowweave.shared.models import FlowRun


def runtime_owner_flow_run_id(db: Session, flow_run_id: str) -> str:
    """Resolve the FlowRun that owns physical Runtime resources.

    Nested automatic records keep their own audit graph, artifacts and events,
    but deliberately execute inside their parent FlowRun Runtime/Workspace.
    Historical top-level automatic runs remain their own Runtime owner.
    """

    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    if run.parent_flow_run_id is None:
        return run.id
    parent = db.get(FlowRun, run.parent_flow_run_id)
    if parent is None:
        raise not_found("flow_run", run.parent_flow_run_id)
    return parent.id


__all__ = ("runtime_owner_flow_run_id",)
