from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from flowweave.modules.admin_control.router import RuntimeReplacementRequest, _request_replacement
from flowweave.modules.sandboxes.application.runtime_sessions import (
    activate_runtime_generation,
    ensure_runtime_generation,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AdminRuntimeOperation,
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    ManagedSandbox,
    TerminalEnvironment,
)


def _seed_active_runtime(db: Session) -> tuple[str, str]:
    image_digest = "sha256:" + "2" * 64
    environment = TerminalEnvironment(
        name=f"admin-control-environment-{uuid4()}",
        description="",
        base_image="python:3.13",
        base_image_digest="sha256:" + "1" * 64,
    )
    db.add(environment)
    db.flush()
    version = EnvironmentVersion(
        environment_id=environment.id,
        version_no=1,
        state="READY",
        base_image_reference="python@sha256:" + "1" * 64,
        base_image_digest="sha256:" + "1" * 64,
        image_reference="flowweave/admin-control-test:v1",
        image_digest=image_digest,
        manifest_json={},
    )
    flow = FlowDefinition(
        name=f"admin-control-flow-{uuid4()}", description="", default_entry_key=None
    )
    db.add_all((version, flow))
    db.flush()
    run = FlowRun(
        flow_definition_id=flow.id,
        run_no=1,
        name="admin control test",
        state="ACTIVE",
        environment_version_id=version.id,
    )
    secret = FlowRunRuntimeSecretReference(
        encrypted_secret_key=b"encrypted",
        secret_digest=uuid4().hex + uuid4().hex,
    )
    db.add_all((run, secret))
    db.flush()
    allocation = FlowRunRuntimeAllocation(
        flow_run_id=run.id,
        secret_reference_id=secret.id,
        relative_root=f".flow-run-runtimes/{run.id}",
    )
    db.add(allocation)
    db.flush()
    runtime = FlowRunRuntime(
        flow_run_id=run.id,
        environment_version_id=version.id,
        runtime_image_digest=image_digest,
        workspace_allocation_id=allocation.id,
        status="STARTING",
    )
    sandbox = ManagedSandbox(
        kind="AGENT_RUNTIME",
        owner_type="FLOW_RUN",
        owner_id=run.id,
        backend="docker",
        backend_resource_name=f"fw-sbx-admin-{run.id[:8]}",
        backend_resource_id=f"admin-instance-{run.id[:8]}",
        desired_state="RUNNING",
        observed_state="RUNNING",
        generation=1,
        image_reference=image_digest,
        runtime_allocation_id=allocation.id,
        spec_json={"port": 8000, "bound": True},
        hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add_all((runtime, sandbox))
    db.flush()
    generation = ensure_runtime_generation(
        db, session=runtime, generation=1, managed_runtime=sandbox
    )
    activate_runtime_generation(
        db,
        session=runtime,
        generation=generation,
        instance_id=sandbox.backend_resource_id,
    )
    db.commit()
    return run.id, runtime.id


def _command(flow_run_id: str, runtime_session_id: str, *, key: str) -> RuntimeReplacementRequest:
    return RuntimeReplacementRequest(
        flow_run_id=flow_run_id,
        runtime_session_id=runtime_session_id,
        expected_generation=1,
        expected_session_row_version=1,
        reason="OpenHands health probe remained unavailable after recovery window.",
        idempotency_key=key,
        actor_user_id="11111111-1111-4111-8111-111111111111",
        actor_username="super-admin",
        request_id="admin-control-test",
    )


def test_admin_replacement_records_audit_and_replays_idempotently(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _seed_active_runtime(db)
        first = _request_replacement(
            db, _command(flow_run_id, runtime_session_id, key="replay-key-000001")
        )
        db.commit()
        assert first["operation"]["idempotent_replay"] is False
        assert first["operation"]["status"] == "SUBMITTED"

    with db_session_factory() as db:
        operation = db.scalar(select(AdminRuntimeOperation))
        runtime = db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.id == runtime_session_id))
        assert operation is not None
        assert operation.reason.startswith("OpenHands health probe")
        assert runtime is not None
        assert runtime.status == "RECONNECTING"
        assert runtime.row_version == 2
        replay = _request_replacement(
            db, _command(flow_run_id, runtime_session_id, key="replay-key-000001")
        )
        assert replay["idempotent_replay"] is True
        assert db.scalar(
            select(AdminRuntimeOperation).where(AdminRuntimeOperation.id == operation.id)
        )


def test_admin_replacement_rejects_reused_key_for_different_command(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _seed_active_runtime(db)
        _request_replacement(
            db, _command(flow_run_id, runtime_session_id, key="conflict-key-00001")
        )
        db.commit()
        with pytest.raises(DomainError, match="idempotency key") as caught:
            _request_replacement(
                db,
                _command(flow_run_id, runtime_session_id, key="conflict-key-00001").model_copy(
                    update={"reason": "A different replacement request must be rejected safely."}
                ),
            )
        assert caught.value.code == "ADMIN_OPERATION_IDEMPOTENCY_CONFLICT"
