from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.infrastructure.docker import DockerSandboxProvider
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.modules.tasks.public import Lease, lease_is_current
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings


def process_flow_run_runtime_pause(
    db: Session,
    flow_run_id: str,
    expected_generation: int,
    lease: Lease,
    *,
    commit: bool = True,
) -> None:
    """Gracefully stop a fenced FlowRun generation without deleting it."""

    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost before Runtime pause")
    session = db.scalar(
        select(FlowRunRuntime)
        .where(
            FlowRunRuntime.flow_run_id == flow_run_id,
            FlowRunRuntime.node_attempt_id.is_(None),
        )
        .with_for_update()
    )
    if (
        session is None
        or session.status != "STOPPED"
        or session.active_generation != expected_generation
    ):
        if commit:
            db.commit()
        else:
            db.flush()
        return
    generation = db.scalar(
        select(RuntimeGeneration)
        .where(
            RuntimeGeneration.runtime_session_id == session.id,
            RuntimeGeneration.generation == expected_generation,
        )
        .with_for_update()
    )
    if generation is None or generation.managed_runtime_id is None:
        raise DomainError(
            "RUNTIME_GENERATION_NOT_ACTIVE",
            "The paused Runtime generation has no managed container",
            409,
            {"runtime_session_id": session.id},
        )
    resource = db.scalar(
        select(ManagedSandbox)
        .where(ManagedSandbox.id == generation.managed_runtime_id)
        .with_for_update()
    )
    if resource is None or resource.desired_state != "STOPPED":
        if commit:
            db.commit()
        else:
            db.flush()
        return
    result = DockerSandboxProvider(get_settings()).drain(resource)
    if not lease_is_current(db, lease):
        raise RuntimeError("task lease was lost during Runtime pause")
    now = datetime.now(UTC)
    transition = db.execute(
        update(RuntimeGeneration)
        .where(
            RuntimeGeneration.id == generation.id,
            RuntimeGeneration.row_version == generation.row_version,
            RuntimeGeneration.state.in_(("READY", "DRAINING")),
        )
        .values(
            state="STOPPED",
            stopped_at=now,
            row_version=RuntimeGeneration.row_version + 1,
            updated_at=now,
        )
    )
    if transition.rowcount != 1:
        raise DomainError(
            "RUNTIME_GENERATION_FENCED",
            "The Runtime pause command is stale",
            409,
            {"runtime_session_id": session.id, "generation": expected_generation},
        )
    resource.observed_state = "STOPPED"
    resource.last_error_code = None
    resource.last_error_detail = None
    # A forced stop is safe too: the Session is already fenced and OpenHands
    # reload on the next start observes the retained persistent state.
    if not result.graceful:
        session.replacement_error_summary = "Runtime paused after an ungraceful drain"
    if commit:
        db.commit()
    else:
        db.flush()


__all__ = ("process_flow_run_runtime_pause",)
