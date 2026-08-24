from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from flowweave.modules.sandboxes.application import runtime_replacement
from flowweave.modules.sandboxes.application.runtime_sessions import (
    acquire_runtime_replacement_lease,
    activate_runtime_generation,
    active_flow_run_runtime_connection,
    assert_active_runtime_fence,
    attach_runtime_replacement_generation,
    ensure_runtime_generation,
    mark_runtime_generation_stopped,
    record_runtime_replacement_failure,
)
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerDrainResult,
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.modules.tasks.public import Lease
from flowweave.runtime.base import RuntimeConversationIdentity
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    FlowRunConversationBinding,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    ManagedSandbox,
    RuntimeGeneration,
    TerminalEnvironment,
)
from flowweave.shared.settings import settings_context


def _seed_active_runtime(db: Session, *, with_conversation: bool = True) -> tuple[str, str]:
    image_digest = "sha256:" + "2" * 64
    environment = TerminalEnvironment(
        name=f"replacement-environment-{uuid4()}",
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
        image_reference="flowweave/replacement-test:v1",
        image_digest=image_digest,
        manifest_json={},
    )
    flow = FlowDefinition(
        name=f"replacement-flow-{uuid4()}",
        description="",
        default_entry_key=None,
        lark_root_folder_url="",
    )
    db.add_all((version, flow))
    db.flush()
    run = FlowRun(
        flow_definition_id=flow.id,
        run_no=1,
        name="replacement test",
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
    source = ManagedSandbox(
        kind="AGENT_RUNTIME",
        owner_type="FLOW_RUN",
        owner_id=run.id,
        backend="docker",
        backend_resource_name=f"fw-sbx-source-{run.id[:8]}",
        backend_resource_id=f"source-instance-{run.id[:8]}",
        desired_state="RUNNING",
        observed_state="RUNNING",
        generation=1,
        image_reference=image_digest,
        runtime_allocation_id=allocation.id,
        spec_json={"port": 8000, "bound": True},
        hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add_all((runtime, source))
    db.flush()
    generation = ensure_runtime_generation(
        db,
        session=runtime,
        generation=1,
        managed_runtime=source,
    )
    activate_runtime_generation(
        db,
        session=runtime,
        generation=generation,
        instance_id=source.backend_resource_id,
    )
    if with_conversation:
        db.add(
            FlowRunConversationBinding(
                flow_run_id=run.id,
                runtime_session_id=runtime.id,
                openhands_conversation_id=str(uuid4()),
                display_label="replacement probe",
            )
        )
    db.commit()
    return run.id, runtime.id


def _identity(conversation_id: str) -> RuntimeConversationIdentity:
    return RuntimeConversationIdentity(
        conversation_id=conversation_id,
        workspace_working_dir="/runtime/workspace/project",
        persistence_dir="/runtime/state/persistence",
        event_id="event-original",
        parent_id="event-parent",
        action_id="action-original",
        tool_call_id="tool-call-original",
    )


def test_replacement_freezes_routes_restores_original_id_and_fences_old_writer(
    settings,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _seed_active_runtime(db)
        old_connection = active_flow_run_runtime_connection(db, flow_run_id=flow_run_id)
        conversation_id = db.scalar(
            select(FlowRunConversationBinding.openhands_conversation_id).where(
                FlowRunConversationBinding.runtime_session_id == runtime_session_id
            )
        )
        assert conversation_id is not None

    routes_frozen_during_drain: list[str] = []
    probes: list[tuple[str, str, RuntimeConversationIdentity | None]] = []
    deleted: list[str] = []

    monkeypatch.setattr(runtime_replacement, "_require_task_lease", lambda *_args: None)
    monkeypatch.setattr(runtime_replacement, "resolve_runtime_secret", lambda *_args: "secret")

    def ensure_running(_provider, target, **_kwargs):
        return DockerObservation(
            resource_id=target.id,
            resource_name=target.backend_resource_name,
            resource_identifier=f"replacement-instance-{target.generation}",
            state="RUNNING",
            labels={},
        )

    def drain(_provider, source):
        with db_session_factory() as check_db:
            with pytest.raises(DomainError) as caught:
                active_flow_run_runtime_connection(check_db, flow_run_id=flow_run_id)
            routes_frozen_during_drain.append(caught.value.code)
        return DockerDrainResult(graceful=True, stopped=True)

    def probe(*, resource_name, conversation_id, expected):
        probes.append((resource_name, conversation_id, expected))
        identity = _identity(conversation_id)
        if expected is not None:
            assert identity == expected
        return identity

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    monkeypatch.setattr(DockerSandboxProvider, "drain", drain)
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete",
        lambda _provider, source: deleted.append(source.backend_resource_name),
    )
    monkeypatch.setattr(runtime_replacement, "_probe_identity", probe)

    with settings_context(settings), db_session_factory() as db:
        runtime_replacement.process_flow_run_runtime_replacement(
            db,
            flow_run_id,
            1,
            Lease(task_id="replacement-task", owner="worker-a", generation=1),
        )

    with db_session_factory() as db:
        runtime = db.get(FlowRunRuntime, runtime_session_id)
        generations = list(
            db.scalars(
                select(RuntimeGeneration)
                .where(RuntimeGeneration.runtime_session_id == runtime_session_id)
                .order_by(RuntimeGeneration.generation)
            )
        )
        current = active_flow_run_runtime_connection(db, flow_run_id=flow_run_id)
        assert runtime is not None
        assert runtime.status == "ACTIVE"
        assert runtime.active_generation == 2
        assert runtime.replacement_generation is None
        assert [item.state for item in generations] == ["DELETED", "READY"]
        assert current.generation == 2
        assert current.managed_runtime_id != old_connection.managed_runtime_id
        with pytest.raises(DomainError) as caught:
            assert_active_runtime_fence(
                db,
                runtime_session_id=old_connection.runtime_session_id,
                generation=old_connection.runtime_fence.generation,
                fence_token=old_connection.runtime_fence.fence_token,
                session_row_version=old_connection.runtime_fence.session_row_version,
                generation_row_version=old_connection.runtime_fence.generation_row_version,
            )
        assert caught.value.code == "RUNTIME_COMMAND_FENCED"

    assert routes_frozen_during_drain == ["RUNTIME_SESSION_NOT_ACTIVE"]
    assert [item[1] for item in probes] == [conversation_id, conversation_id]
    assert probes[0][2] is None
    assert probes[1][2] == _identity(conversation_id)
    assert len(deleted) == 1


def test_crash_takeover_reuses_n_plus_one_and_waits_for_openhands_lease(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _seed_active_runtime(db, with_conversation=False)
        runtime, first = acquire_runtime_replacement_lease(
            db,
            flow_run_id=flow_run_id,
            owner="worker-a",
            lease_seconds=60,
        )
        source = db.scalar(
            select(ManagedSandbox).where(
                ManagedSandbox.owner_type == "FLOW_RUN",
                ManagedSandbox.owner_id == flow_run_id,
                ManagedSandbox.generation == 1,
            )
        )
        assert source is not None
        target_runtime = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="FLOW_RUN",
            owner_id=flow_run_id,
            backend="docker",
            backend_resource_name=f"fw-sbx-target-{flow_run_id[:8]}",
            desired_state="RUNNING",
            observed_state="CREATING",
            generation=2,
            image_reference=runtime.runtime_image_digest,
            runtime_allocation_id=runtime.workspace_allocation_id,
            spec_json=dict(source.spec_json),
            hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(target_runtime)
        db.flush()
        target = ensure_runtime_generation(
            db,
            session=runtime,
            generation=2,
            managed_runtime=target_runtime,
        )
        attach_runtime_replacement_generation(
            db,
            session=runtime,
            generation=target,
            replacement_lease_token=first.token,
        )
        runtime.replacement_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    with db_session_factory() as db:
        runtime, takeover = acquire_runtime_replacement_lease(
            db,
            flow_run_id=flow_run_id,
            owner="worker-b",
            lease_seconds=60,
        )
        assert takeover.token != first.token
        assert takeover.target_generation == 2
        generations = list(
            db.scalars(
                select(RuntimeGeneration).where(
                    RuntimeGeneration.runtime_session_id == runtime_session_id
                )
            )
        )
        assert len(generations) == 2
        source_generation = next(item for item in generations if item.generation == 1)
        mark_runtime_generation_stopped(
            db,
            session=runtime,
            generation=source_generation,
            replacement_lease_token=takeover.token,
            graceful=False,
        )
        assert runtime.replacement_not_before is not None
        wait_seconds = (runtime.replacement_not_before - datetime.now(UTC)).total_seconds()
        assert 44 <= wait_seconds <= 45


def test_replacement_failure_isolated_and_retryability_is_explicit(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        failed_run_id, failed_session_id = _seed_active_runtime(db, with_conversation=False)
        healthy_run_id, _healthy_session_id = _seed_active_runtime(db, with_conversation=False)
        failed, retry_lease = acquire_runtime_replacement_lease(
            db,
            flow_run_id=failed_run_id,
            owner="worker-a",
            lease_seconds=60,
        )
        with pytest.raises(DomainError) as caught:
            active_flow_run_runtime_connection(db, flow_run_id=failed_run_id)
        assert caught.value.code == "RUNTIME_SESSION_NOT_ACTIVE"
        assert active_flow_run_runtime_connection(db, flow_run_id=healthy_run_id).generation == 1

        record_runtime_replacement_failure(
            db,
            session=failed,
            replacement_lease_token=retry_lease.token,
            error_code="RUNTIME_CONNECTION_ERROR",
            error_summary="redacted network partition",
            retryable=True,
        )
        assert failed.status == "RECONNECTING"
        assert failed.replacement_error_code == "RUNTIME_CONNECTION_ERROR"
        assert failed.replacement_lease_token is None
        db.commit()

    with db_session_factory() as db:
        failed, terminal_lease = acquire_runtime_replacement_lease(
            db,
            flow_run_id=failed_run_id,
            owner="worker-b",
            lease_seconds=60,
        )
        record_runtime_replacement_failure(
            db,
            session=failed,
            replacement_lease_token=terminal_lease.token,
            error_code="RUNTIME_EVENT_IDENTITY_DRIFT",
            error_summary="redacted identity mismatch",
            retryable=False,
        )
        db.commit()

    with db_session_factory() as db:
        failed = db.get(FlowRunRuntime, failed_session_id)
        assert failed is not None
        assert failed.status == "DEGRADED"
        assert failed.replacement_error_code == "RUNTIME_EVENT_IDENTITY_DRIFT"
        assert active_flow_run_runtime_connection(db, flow_run_id=healthy_run_id).generation == 1
