"""Start reservations use the caller transaction, never a physical provider.

Set FR570_TEST_DATABASE_URL to an isolated PostgreSQL database to exercise
real row locks. Each PostgreSQL fixture owns a unique schema; production
settings/database URLs are never used as a fallback.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import flow_node_host
from flowweave.modules.agent_sessions.application.runtime_config import FrozenSessionConfig
from flowweave.modules.orchestration.application import service as orchestration
from flowweave.runtime.contract import OPENHANDS_PACKAGE_VERSIONS
from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.shared.application.transactions import mark_uow_owned
from flowweave.shared.database import Base
from flowweave.shared.domain.openhands import OPENHANDS_SOURCE_COMMIT
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    AgentConversationBinding,
    BackgroundTask,
    FlowRun,
    FlowRunRuntime,
    NodeAttempt,
    NodeRun,
    RunSnapshot,
)
from flowweave.shared.schemas import StepwiseNodeDraftStartWrite
from flowweave.shared.settings import settings_context


@pytest.fixture
def start_context(tmp_path, monkeypatch, request):
    url = os.environ.get("FR570_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set FR570_TEST_DATABASE_URL to an isolated PostgreSQL database")
    engine = create_engine(url)
    schema = f"fr570_{uuid4().hex}"
    assert engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine.dispose()

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        connection.execute(f'SET search_path TO "{schema}"')
        connection.execute("SET statement_timeout TO '1000ms'")
        connection.commit()

    def cleanup():
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            engine.dispose()

    request.addfinalizer(cleanup)

    metadata = MetaData()
    for name in (
        "flow_runs",
        "run_snapshots",
        "node_runs",
        "node_attempts",
        "attempt_input_bindings",
        "flow_run_runtime_secret_references",
        "flow_run_runtime_allocations",
        "flow_run_runtimes",
        "terminal_environments",
        "environment_versions",
        "agent_conversation_bindings",
        "agent_conversation_capabilities",
        "human_actions",
        "run_events",
        "background_tasks",
    ):
        Base.metadata.tables[name].to_metadata(metadata)
    metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        runtime_adapter="openhands",
        execution_mode="worker",
        credentials_master_key="Qy0d9T_0Y4GxN31PqYqzRo6YD_s-hnbJFRb_v8xQwFc=",
        workspace_root=tmp_path / "workspace",
    )
    settings.workspace_root.mkdir()

    def no_physical_runtime(*_args, **_kwargs):
        pytest.fail("Attempt confirmation must not provision or inspect a physical Runtime")

    for name in (
        "ensure_flow_run_runtime",
        "ensure_node_attempt_runtime",
        "active_node_runtime_connection",
    ):
        monkeypatch.setattr(flow_node_host.sandboxes, name, no_physical_runtime)
    config = FrozenSessionConfig(None, "test-provider", "test-model", None, ())
    monkeypatch.setattr(
        orchestration.agent_sessions, "resolve_session_config", lambda *_args, **_kwargs: config
    )
    # Event-trigger delivery and the full display projection are independent
    # of the Attempt/Binding/outbox transaction under test.
    monkeypatch.setattr(orchestration, "enqueue_matching_deliveries", lambda *_args: None)

    def detail(db, attempt_id):
        attempt = db.get(NodeAttempt, attempt_id)
        binding = db.scalar(
            select(AgentConversationBinding).where(
                AgentConversationBinding.node_attempt_id == attempt_id
            )
        )
        return {"id": attempt.id, "state": attempt.state, "binding_id": binding.id}

    monkeypatch.setattr(orchestration, "attempt_detail", detail)
    with settings_context(settings):
        yield SimpleNamespace(engine=engine)


def seed_record(db, runtime_status="ACTIVE"):
    from flowweave.shared.models import EnvironmentVersion, TerminalEnvironment

    environment = TerminalEnvironment(name="test-environment", base_image="test")
    db.add(environment)
    db.flush()
    version = EnvironmentVersion(
        environment_id=environment.id,
        version_no=1,
        state="READY",
        image_digest="sha256:" + "1" * 64,
        manifest_json={
            "image_id": "sha256:" + "1" * 64,
            "runtime_provenance": {
                "package_versions": dict(OPENHANDS_PACKAGE_VERSIONS),
                "source_commit": OPENHANDS_SOURCE_COMMIT,
                "source_ref": OPENHANDS_SOURCE_COMMIT,
            },
            "build": {
                "builder": "openhands.agent_server.docker.build",
                "target": "source-minimal",
                "platform": "linux/amd64",
                "user_base_image_reference": "test-base@sha256:" + "2" * 64,
                "user_base_image_digest": "sha256:" + "2" * 64,
                "runtime_image_digest": "sha256:" + "1" * 64,
            },
            "validation": {
                "contract_check": {"status": "PASSED"},
                "tool_workspace_probe": {"status": "PASSED"},
                "security_scan": {"status": "PASSED"},
            },
        },
    )
    db.add(version)
    db.flush()
    parent = FlowRun(
        flow_definition_id=str(uuid4()),
        run_no=1,
        name="shared-parent",
        environment_version_id=version.id,
    )
    db.add(parent)
    db.flush()
    record = FlowRun(
        flow_definition_id=parent.flow_definition_id,
        parent_flow_run_id=parent.id,
        run_no=1,
        name="private-record",
        environment_version_id=version.id,
        automation_plan_json={
            "node_drafts": {
                "collect": {
                    "row_version": 1,
                    "startup_mode": "PROMPT",
                    "startup_prompt": "Run the node",
                    "agent_preset": {
                        "model_provider_id": "test-provider",
                        "model_name": "test-model",
                    },
                    "gates": [],
                    "context_ids": [],
                }
            }
        },
    )
    db.add(record)
    db.flush()
    asset_id = str(uuid4())
    manifest = {"schema_version": 3, "nodes": {"collect": {"node_asset_id": asset_id}}}
    snapshot = RunSnapshot(
        flow_run_id=record.id,
        version=1,
        schema_version=2,
        definition_json={
            "nodes": [
                {"instance_key": "collect", "asset": {"id": asset_id, "inputs": [], "outputs": []}}
            ]
        },
        definition_hash="test-definition",
        runtime_manifest_json=manifest,
        runtime_manifest_hash=runtime_manifest_hash(manifest),
        environment_version_id=version.id,
    )
    db.add(snapshot)
    db.flush()
    record.active_snapshot_id = snapshot.id
    orchestration._create_stepwise_record_runtime(db, record, version, snapshot)
    runtime = db.scalar(select(FlowRunRuntime).where(FlowRunRuntime.flow_run_id == parent.id))
    runtime.status = runtime_status
    db.commit()
    return parent.id, record.id, runtime.id


def start(db, record_id, key="test-start", version=1):
    return orchestration.start_stepwise_node_draft(
        db, record_id, "collect", StepwiseNodeDraftStartWrite(expected_row_version=version), key
    )


@pytest.mark.parametrize("runtime_status", ["ACTIVE", "STARTING", "RECONNECTING"])
def test_start_reserves_binding_and_task_without_runtime_io(start_context, runtime_status):
    with Session(start_context.engine, expire_on_commit=False) as db:
        _parent_id, record_id, runtime_id = seed_record(db, runtime_status)
        result = start(db, record_id)
        assert result["state"] == "EXECUTING"
        binding = db.get(AgentConversationBinding, result["binding_id"])
        assert binding.runtime_session_id == runtime_id
        assert binding.flow_run_id == record_id
        assert binding.lifecycle == "PROVISIONING"
        assert db.get(FlowRunRuntime, runtime_id).status == runtime_status
        assert start(db, record_id) == result
        assert db.scalar(select(func.count(NodeAttempt.id))) == 1
        assert db.scalar(select(func.count(AgentConversationBinding.id))) == 1
        tasks = list(db.scalars(select(BackgroundTask)))
        assert [(task.task_type, task.aggregate_id) for task in tasks] == [
            ("START_RUNTIME", result["id"])
        ]


def test_failed_configuration_rolls_back_entire_start_reservation(start_context, monkeypatch):
    with Session(start_context.engine, expire_on_commit=False) as db:
        _parent_id, record_id, _runtime_id = seed_record(db)

        def reject_config(*_args, **_kwargs):
            raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "Choose a model", 409)

        monkeypatch.setattr(orchestration.agent_sessions, "resolve_session_config", reject_config)
        with pytest.raises(DomainError, match="Choose a model"):
            start(db, record_id)
        db.rollback()
        for model in (NodeRun, NodeAttempt, AgentConversationBinding, BackgroundTask):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_stale_draft_version_creates_no_attempt(start_context):
    with Session(start_context.engine, expire_on_commit=False) as db:
        _parent_id, record_id, _runtime_id = seed_record(db)
        with pytest.raises(DomainError) as rejected:
            start(db, record_id, version=2)
        assert rejected.value.code == "VERSION_CONFLICT"
        db.rollback()
        assert db.scalar(select(func.count(NodeAttempt.id))) == 0


def test_outer_uow_can_roll_back_binding_attempt_and_task_together(start_context):
    with Session(start_context.engine, expire_on_commit=False) as db:
        _parent_id, record_id, _runtime_id = seed_record(db)
        mark_uow_owned(db)
        result = start(db, record_id)
        assert db.get(AgentConversationBinding, result["binding_id"]) is not None
        db.rollback()
    with Session(start_context.engine) as db:
        for model in (NodeRun, NodeAttempt, AgentConversationBinding, BackgroundTask):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_postgres_start_does_not_wait_on_its_own_parent_row_lock(start_context):
    with Session(start_context.engine, expire_on_commit=False) as db:
        parent_id, record_id, _runtime_id = seed_record(db)
        db.scalar(select(FlowRun.id).where(FlowRun.id == parent_id).with_for_update())
        # Prove another connection cannot acquire the exact lock still held by
        # the caller. This is the formerly failing independent-control boundary.
        with Session(start_context.engine) as other:
            with pytest.raises(OperationalError) as blocked:
                other.scalar(select(FlowRun.id).where(FlowRun.id == parent_id).with_for_update())
            assert blocked.value.orig.sqlstate == "57014"
            other.rollback()
        result = start(db, record_id)
        assert result["state"] == "EXECUTING"
