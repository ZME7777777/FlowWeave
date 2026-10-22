"""Record creation freezes profile defaults and carries them to native execution."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from flowweave.modules.orchestration.application import service
from flowweave.modules.runs.application.git_settings import record_git_identity
from flowweave.modules.users.application.security import tenant_user
from flowweave.modules.users.infrastructure.models import User
from flowweave.runtime.base import RuntimeAgentSpec
from flowweave.runtime.request import build_runtime_request
from flowweave.shared.models import FlowRun, NodeRun, RunSnapshot
from flowweave.shared.schemas import (
    AutomaticRunDraftWrite,
    GitIdentityWrite,
    StepwiseRunRecordWrite,
)


@pytest.fixture
def records(monkeypatch):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.create_function("set_config", 3, lambda _key, value, _local: value)

    for model in (User, FlowRun, NodeRun, RunSnapshot):
        model.__table__.create(engine)
    definition = {"nodes": [{"instance_key": "first", "asset": {"inputs": []}}], "edges": []}
    environment = SimpleNamespace(
        id="environment", image_digest="digest", runtime_protocol_profile="native", manifest_json={}
    )
    monkeypatch.setattr(service, "lock_referenceable_version", lambda *_args: environment)
    monkeypatch.setattr(service, "_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "hold_snapshot_memory_references", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "runtime_server_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_snapshot_definition", lambda *_args, **_kwargs: definition)
    monkeypatch.setattr(service, "_compile_runtime_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "load_flow", lambda *_args: SimpleNamespace(name="Flow"))
    monkeypatch.setattr(service, "finish", lambda db: db.commit())
    monkeypatch.setattr(service, "run_detail", lambda db, record_id: {"id": record_id})
    with Session(engine, expire_on_commit=False) as db, tenant_user("owner"):
        db.add(
            User(
                id="owner",
                username="login-account",
                role="USER",
                display_name="真实姓名",
                email="name@example.test",
            )
        )
        parent = FlowRun(
            id="parent",
            flow_definition_id="flow",
            run_no=1,
            name="Parent",
            environment_version_id="environment",
        )
        db.add(parent)
        snapshot = RunSnapshot(
            id="snapshot",
            flow_run_id="parent",
            version=1,
            definition_json=definition,
            definition_hash="hash",
            runtime_manifest_json={},
            runtime_manifest_hash="manifest",
            environment_version_id="environment",
        )
        db.add(snapshot)
        db.flush()
        parent.active_snapshot_id = snapshot.id
        db.commit()
        yield db
    engine.dispose()


def test_stepwise_and_continuous_records_freeze_profile_and_manual_override(records, monkeypatch):
    db = records
    first = service._create_nested_stepwise_run_record(
        db, "parent", StepwiseRunRecordWrite(start_node_key="first")
    )
    second = service.create_nested_automatic_run_draft(
        db,
        "parent",
        AutomaticRunDraftWrite(
            environment_version_id="environment",
            start_node_key="first",
            git_identity=GitIdentityWrite(author="Override", email="override@example.test"),
        ),
    )
    original = {"author": "真实姓名", "email": "name@example.test"}
    assert record_git_identity(first) == original
    other = db.get(FlowRun, second["id"])
    assert record_git_identity(other) == {"author": "Override", "email": "override@example.test"}
    db.get(User, "owner").display_name = "Later Profile"
    # A later automatic draft save must retain the record's original identity.
    from flowweave.shared.schemas import AutomaticRunDraftUpdateWrite

    service.update_automatic_run_draft(
        db,
        other.id,
        AutomaticRunDraftUpdateWrite(
            expected_row_version=other.row_version, start_node_key="first", node_plans={}
        ),
    )
    db.commit()
    db.expire_all()
    assert record_git_identity(db.get(FlowRun, first.id)) == original
    assert record_git_identity(db.get(FlowRun, other.id))["author"] == "Override"
    monkeypatch.setattr("flowweave.runtime.request.credentials_for_agent", lambda _db: ({}, ""))
    monkeypatch.setattr(
        "flowweave.runtime.request.node_attempt_workspace_context",
        lambda _db, **kwargs: SimpleNamespace(
            runtime_working_directory="/runtime/workspace/project/" + kwargs["flow_run_id"]
        ),
    )
    for record, author in ((first, "真实姓名"), (other, "Override")):
        request = build_runtime_request(
            db,
            flow_run_id=record.id,
            runtime_manifest_hash="manifest",
            attempt_id="attempt",
            execution_key="create",
            node={},
            bindings=[],
            workspace_ref="unused",
            interaction_mode="COLLABORATION",
            agent_spec=RuntimeAgentSpec(),
            node_attempt_id="attempt",
        )
        assert request.git_identity[0] == author
        assert request.workspace_root.endswith("/" + record.id)


@pytest.mark.parametrize(
    "value",
    [
        {"author": "  ", "email": "a@example.test"},
        {"author": "bad\nname", "email": "a@example.test"},
        {"author": "Name", "email": "invalid"},
        {"author": "Name", "email": "a@example.test", "root": "/other-record"},
    ],
)
def test_record_identity_rejects_invalid_names_addresses_and_paths(value):
    with pytest.raises(ValidationError):
        GitIdentityWrite.model_validate(value)


def test_copy_and_portable_export_keep_record_identity_after_profile_changes(records):
    db = records
    from flowweave.shared.schemas import (
        AutomaticRecordConfigExportWrite,
        AutomaticRecordConfigImportWrite,
        AutomaticRunCopyWrite,
        StepwiseRecordConfigExportWrite,
        StepwiseRunRecordCopyWrite,
    )

    original = {"author": "真实姓名", "email": "name@example.test"}
    step = service._create_nested_stepwise_run_record(
        db, "parent", StepwiseRunRecordWrite(start_node_key="first")
    )
    auto = service.create_nested_automatic_run_draft(
        db,
        "parent",
        AutomaticRunDraftWrite(environment_version_id="environment", start_node_key="first"),
    )
    db.get(User, "owner").display_name = "Changed Profile"
    copied_step = service.copy_nested_stepwise_run_record(
        db, "parent", step.id, StepwiseRunRecordCopyWrite(name="Step copy")
    )
    copied_auto = service.copy_automatic_run_draft(
        db, auto["id"], AutomaticRunCopyWrite(name="Auto copy")
    )
    for copied in (copied_step, copied_auto):
        assert record_git_identity(db.get(FlowRun, copied["id"])) == original
    step_export = service.export_nested_stepwise_run_configs(
        db, "parent", StepwiseRecordConfigExportWrite(record_ids=[step.id])
    )
    auto_export = service.export_nested_automatic_run_configs(
        db, "parent", AutomaticRecordConfigExportWrite(record_ids=[auto["id"]])
    )
    for exported in (step_export, auto_export):
        assert exported["records"][0]["git_identity"] == original
    imported = service.import_nested_automatic_run_configs(
        db, "parent", AutomaticRecordConfigImportWrite.model_validate(auto_export)
    )
    assert record_git_identity(db.get(FlowRun, imported[0]["id"])) == original
