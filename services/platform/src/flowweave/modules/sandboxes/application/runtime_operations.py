from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.application.runtime_replacement import (
    enqueue_flow_run_runtime_replacement,
)
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    RuntimeGeneration,
)
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, not_found
from flowweave.shared.models import FlowRun


def _retention_policy() -> dict[str, Any]:
    return {
        "mode": "FLOW_RUN_LIFETIME",
        "workspace_preserved_during_replacement": True,
        "physical_delete_operation": "DELETE_FLOW_RUN",
    }


def runtime_overview(db: Session, flow_run_id: str) -> dict[str, Any]:
    """Return logical Runtime health without exposing a physical connection."""

    run = db.get(FlowRun, flow_run_id)
    if run is None:
        raise not_found("flow_run", flow_run_id)
    session = db.scalar(
        select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == flow_run_id)
    )
    if session is None:
        return {
            "flow_run_id": flow_run_id,
            "runtime_session_id": None,
            "status": "ARCHIVED",
            "connection_state": "ARCHIVED",
            "active_generation": None,
            "replacement_generation": None,
            "session_row_version": None,
            "write_available": False,
            "read_only": True,
            "rerun_required": True,
            "diagnostic_code": "LEGACY_RUNTIME_INCOMPATIBLE",
            "diagnostic_summary": (
                "Historical Runtime data is intentionally incompatible; rerun the Flow."
            ),
            "generations": [],
            "retention": _retention_policy(),
        }
    generations = list(
        db.scalars(
            select(RuntimeGeneration)
            .where(RuntimeGeneration.runtime_session_id == session.id)
            .order_by(RuntimeGeneration.generation.desc())
        )
    )
    connection_state = (
        session.status
        if session.status in {"RECONNECTING", "DEGRADED"}
        else "READY"
        if session.status == "ACTIVE"
        else session.status
    )
    return {
        "flow_run_id": flow_run_id,
        "runtime_session_id": session.id,
        "status": session.status,
        "connection_state": connection_state,
        "active_generation": session.active_generation,
        "replacement_generation": session.replacement_generation,
        "session_row_version": session.row_version,
        "write_available": session.status == "ACTIVE",
        "read_only": session.status in {"STOPPED", "DELETING"},
        "rerun_required": False,
        "diagnostic_code": session.replacement_error_code,
        "diagnostic_summary": session.replacement_error_summary,
        "generations": [
            {
                "generation": item.generation,
                "state": item.state,
                "row_version": item.row_version,
                "failure_code": item.failure_code,
                "failure_summary": item.failure_summary,
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "ready_at": item.ready_at.isoformat() if item.ready_at else None,
                "draining_at": item.draining_at.isoformat() if item.draining_at else None,
                "stopped_at": item.stopped_at.isoformat() if item.stopped_at else None,
                "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
            }
            for item in generations
        ],
        "retention": _retention_policy(),
    }


def request_runtime_replacement(
    db: Session,
    flow_run_id: str,
    *,
    expected_generation: int,
    expected_session_row_version: int,
) -> dict[str, Any]:
    """Fence writes immediately and enqueue one generation-scoped replacement."""

    session = db.scalar(
        select(FlowRunRuntime)
        .where(FlowRunRuntime.flow_run_id == flow_run_id)
        .with_for_update()
    )
    if session is None or session.active_generation is None:
        raise DomainError(
            "RUNTIME_SESSION_NOT_ACTIVE",
            "The FlowRun has no active generation to replace",
            409,
            {"flow_run_id": flow_run_id},
        )
    if (
        session.active_generation != expected_generation
        or session.row_version != expected_session_row_version
    ):
        raise conflict(
            "Runtime Session was modified",
            expected_generation=expected_generation,
            actual_generation=session.active_generation,
            expected_session_row_version=expected_session_row_version,
            actual_session_row_version=session.row_version,
        )
    if session.status not in {"ACTIVE", "DEGRADED"}:
        raise DomainError(
            "RUNTIME_REPLACEMENT_NOT_ALLOWED",
            "The Runtime Session does not allow an operator replacement",
            409,
            {"runtime_session_id": session.id, "status": session.status},
        )
    session.status = "RECONNECTING"
    session.replacement_error_code = None
    session.replacement_error_summary = None
    session.row_version += 1
    session.updated_at = now()
    enqueue_flow_run_runtime_replacement(
        db,
        flow_run_id=flow_run_id,
        failed_generation=expected_generation,
        reason="OPERATOR_REQUEST",
        request_key=f"operator-{expected_session_row_version}",
    )
    finish(db)
    return runtime_overview(db, flow_run_id)


__all__ = ("request_runtime_replacement", "runtime_overview")
