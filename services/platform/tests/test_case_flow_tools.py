"""PostgreSQL/HTTP authority, draft lifecycle and action audit regressions."""

import copy
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from test_case_native_tools import native_case  # noqa: F401

from flowweave.modules.cases.application import native_tools, service
from flowweave.modules.cases.infrastructure.models import (
    CaseControlPlan,
    CaseFlowRun,
    CaseFlowToolGrant,
    CaseStage,
)
from flowweave.modules.flows.infrastructure.models import FlowDefinition
from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.runs.infrastructure.models import FlowRun, NodeRun, RunSnapshot
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.credentials_crypto import encrypt_secret
from flowweave.shared.errors import DomainError


@pytest.fixture
def flow_case(native_case, monkeypatch):  # noqa: F811
    g = native_case
    db = g.db
    monkeypatch.setattr(service, "write_case_files", lambda *args: None)
    flow = FlowDefinition(name="Scene tools graph")
    provider = ModelProvider(
        name="Scene provider",
        base_url="https://model.example.test/v1",
        encrypted_api_key=encrypt_secret("test-provider-key"),
        connection_state="CONNECTED",
    )
    db.add_all([flow, provider])
    db.flush()
    db.add(ProviderModel(provider_id=provider.id, model_name="test-model", enabled=True))
    g.preset = {"model_provider_id": provider.id, "model_name": "test-model"}
    g.definition = {
        "name": "Scene graph",
        "nodes": [
            {
                "instance_key": key,
                "alias": key,
                "asset": {
                    "id": str(uuid4()),
                    "name": "Node " + key,
                    "description": "input contract",
                    "inputs": [
                        {"field_key": "link", "data_type": "URL", "display_name": "reference"}
                    ],
                    "outputs": [
                        {"field_key": "report", "data_type": "URL", "display_name": "report"}
                    ],
                    "executor": {
                        "startup_prompt": "check input",
                        "context_prompt": "cite evidence",
                    },
                },
            }
            for key in ("first", "second")
        ],
        "edges": [{"source_instance_key": "first", "target_instance_key": "second", "position": 0}],
        "port_mappings": [
            {
                "source_instance_key": "first",
                "source_output_key": "report",
                "target_instance_key": "second",
                "target_input_key": "link",
            }
        ],
    }
    plans = {
        key: {
            "startup_prompt": "original " + key,
            "agent_preset": g.preset,
            "artifact_ids": {},
            "input_urls": {"link": "https://example.test/input"},
            "gates": [],
        }
        for key in ("first", "second")
    }
    runs = []
    for number in (1, 2):
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=number,
            name="template" if number == 1 else "execution",
            run_mode="AUTOMATIC",
            state="DRAFT",
            environment_version_id=g.client.environment_version_id,
            automation_plan_json={
                "start_node_key": "first",
                "node_plans": copy.deepcopy(plans),
                "readiness": {"ready": True},
            },
        )
        db.add(run)
        db.flush()
        snapshot = RunSnapshot(
            flow_run_id=run.id,
            version=1,
            definition_json=copy.deepcopy(g.definition),
            definition_hash="a" * 64,
            runtime_manifest_json={"schema_version": 3, "nodes": {}},
            runtime_manifest_hash="b" * 64,
            environment_version_id=run.environment_version_id,
        )
        db.add(snapshot)
        db.flush()
        run.active_snapshot_id = snapshot.id
        runs.append(run)
    g.template, g.execution = runs
    g.stage = db.scalar(
        select(CaseStage).where(CaseStage.case_id == g.item.id, CaseStage.stage_key == "0")
    )
    g.stage.execution_mode = "CONTINUOUS"
    g.stage.flow_run_id = g.template.id
    g.stage.current_execution_flow_run_id = g.execution.id
    db.add(CaseFlowRun(case_id=g.item.id, flow_run_id=g.execution.id))
    db.commit()
    g.flow_token = native_tools.prepare_creation(
        db, g.entry.id, g.binding, contract=native_tools.FLOWS
    )[native_tools.FLOWS.secret_name]
    yield g


def rpc(g, name, args=None, *, token=None, path=None):
    return g.client.post(
        path or native_tools.FLOWS.path,
        headers={"Authorization": "Bearer " + (token or g.flow_token)},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        },
    )


def projection(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return json.loads(body["result"]["content"][0]["text"])


def action(g, name="START_EXECUTION", **overrides):
    # Read the exact current preconditions via the same native tool surface.
    facts = projection(
        rpc(
            g,
            "read_case_flow",
            {"source": "TEMPLATE" if name == "CREATE_EXECUTION_COPY" else "CURRENT"},
        )
    )
    value = {
        "action": name,
        "idempotency_key": "action:" + str(uuid4()),
        "reason": "check scene inputs",
        "expected_case_row_version": facts["case_row_version"],
        "expected_stage_key": facts["stage_key"],
        "expected_execution_flow_run_id": facts["current_execution_flow_run_id"],
        "expected_run_row_version": facts["flow_run"]["row_version"],
        "expected_snapshot_version": facts["flow_run"]["active_snapshot_version"],
    }
    value.update(overrides)
    return value


def refresh(g):
    g.db.expire_all()


def test_separate_native_contracts_and_schema_have_no_identity_overrides(flow_case):
    g = flow_case
    assert rpc(g, "read_case_flow", token=g.token).status_code == 401
    assert (
        rpc(g, "read_case_context", token=g.flow_token, path=native_tools.MCP_PATH).status_code
        == 401
    )
    assert rpc(g, "manage_case_flow", token=g.token).status_code == 401
    listed = g.client.post(
        native_tools.FLOWS.path,
        headers={"Authorization": "Bearer " + g.flow_token},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ).json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["read_case_flow", "manage_case_flow"]
    assert [tool["annotations"]["readOnlyHint"] for tool in listed] == [True, False]
    assert "case_id" not in listed[1]["inputSchema"]["properties"]
    refs = g.db.scalars(select(CaseFlowToolGrant)).all()
    assert len(refs) == 1
    assert refs[0].capability_digest != g.version.digest


def test_bounded_graph_contract_and_draft_reads_do_not_leak_manifests(flow_case):
    g = flow_case
    nodes = projection(rpc(g, "read_case_flow", {"section": "nodes", "limit": 1}))
    assert nodes["total"] == 2 and nodes["next_offset"] == 1
    assert nodes["items"][0]["asset"]["inputs"][0]["field_key"] == "link"
    assert nodes["items"][0]["asset"]["executor"]["startup_prompt"] == "check input"
    assert len(projection(rpc(g, "read_case_flow", {"section": "edges"}))["items"]) == 2
    end = projection(rpc(g, "read_case_flow", {"section": "nodes", "offset": 1}))
    assert end["next_offset"] is None and end["items"][0]["instance_key"] == "second"
    drafts = projection(rpc(g, "read_case_flow", {"section": "drafts", "flow_node_key": "first"}))
    assert drafts["total"] == 1 and drafts["items"][0]["startup_prompt"] == "original first"
    assert "capabilities" not in drafts["items"][0]["agent_preset"]
    assert "runtime_manifest" not in json.dumps(nodes)
    hidden = projection(rpc(g, "read_case_flow", {"stage_key": "foreign"}))
    assert hidden["error_code"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_id", "foreign"),
        ("flow_run_id", "foreign"),
        ("binding_id", "foreign"),
        ("offset", True),
        ("limit", 51),
    ],
)
def test_read_arguments_cannot_expand_authority(flow_case, field, value):
    assert rpc(flow_case, "read_case_flow", {field: value}).json()["error"]["code"] == -32602


def test_real_copy_is_independent_and_response_retry_never_duplicates(flow_case):
    g = flow_case
    g.stage.current_execution_flow_run_id = None
    g.db.commit()
    original = copy.deepcopy(g.template.automation_plan_json)
    args = action(g, "CREATE_EXECUTION_COPY")
    first = projection(rpc(g, "manage_case_flow", args))
    assert first["state"] == "SUCCEEDED", first
    second = projection(rpc(g, "manage_case_flow", args))
    assert second == first
    refresh(g)
    copied_id = g.stage.current_execution_flow_run_id
    assert copied_id not in {g.template.id, g.execution.id}
    assert g.db.get(FlowRun, copied_id).state == "DRAFT"
    assert g.template.automation_plan_json == original
    assert (
        g.db.scalar(
            select(CaseControlPlan).where(CaseControlPlan.id == first["plan_id"])
        ).result_json["case"]["id"]
        == g.item.id
    )
    assert len(g.db.scalars(select(FlowRun)).all()) == 3


def test_real_automatic_configuration_preserves_template_and_can_be_read_back(flow_case):
    g = flow_case
    original = copy.deepcopy(g.template.automation_plan_json)
    plans = copy.deepcopy(g.execution.automation_plan_json["node_plans"])
    plans["first"]["startup_prompt"] = "Agent chose complete inputs"
    args = action(
        g,
        "CONFIGURE_EXECUTION",
        automatic_draft={"expected_row_version": 1, "start_node_key": "first", "node_plans": plans},
    )
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "SUCCEEDED", saved
    assert projection(rpc(g, "manage_case_flow", args)) == saved
    refresh(g)
    assert g.execution.row_version == 2
    assert g.template.automation_plan_json == original
    drafts = projection(rpc(g, "read_case_flow", {"section": "drafts"}))["items"]
    assert drafts[0]["startup_prompt"] == "Agent chose complete inputs"


@pytest.mark.parametrize("changed", ["case", "stage", "execution", "run", "snapshot"])
def test_stale_preconditions_are_durably_failed_and_never_start(flow_case, monkeypatch, changed):
    g = flow_case
    calls = []
    monkeypatch.setattr(
        service.orchestration, "start_automatic_run", lambda *args: calls.append(args)
    )
    args = action(g)
    if changed == "case":
        args["expected_case_row_version"] += 1
    elif changed == "stage":
        args["expected_stage_key"] = "1"
    elif changed == "execution":
        args["expected_execution_flow_run_id"] = g.template.id
    elif changed == "run":
        args["expected_run_row_version"] += 1
    else:
        args["expected_snapshot_version"] += 1
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "FAILED" and saved["error_code"] == "CASE_FLOW_VERSION_CONFLICT"
    assert projection(rpc(g, "manage_case_flow", args)) == saved
    refresh(g)
    stored = g.db.get(CaseControlPlan, saved["plan_id"])
    assert (
        stored.state == "FAILED"
        and stored.input_snapshot_json["requested"]["expected_stage_key"]
        == args["expected_stage_key"]
    )
    assert not calls


def test_duplicate_start_and_payload_reuse_are_distinct(flow_case, monkeypatch):
    g = flow_case
    calls = []

    def start(db, run_id, payload, key):
        calls.append(key)
        db.get(FlowRun, run_id).state = "ACTIVE"
        return {"id": run_id, "state": "ACTIVE"}

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)
    args = action(g)
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "SUCCEEDED", saved
    assert projection(rpc(g, "manage_case_flow", args)) == saved
    assert len(calls) == 1
    changed = {**args, "reason": "different decision"}
    assert projection(rpc(g, "manage_case_flow", changed))["error_code"] == "VERSION_CONFLICT"
    assert len(calls) == 1


def test_failed_start_requires_linked_new_attempt(flow_case, monkeypatch):
    g = flow_case
    calls = []

    def start(db, run_id, payload, key):
        calls.append(key)
        if len(calls) == 1:
            raise DomainError("TEMPORARY_PROVIDER_ERROR", "test failure", 503)
        db.get(FlowRun, run_id).state = "ACTIVE"
        return {"id": run_id, "state": "ACTIVE"}

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)
    args = action(g)
    failed = projection(rpc(g, "manage_case_flow", args))
    assert failed["state"] == "FAILED"
    assert projection(rpc(g, "manage_case_flow", args)) == failed
    retry = action(g, retry_of_plan_id=failed["plan_id"])
    saved = projection(rpc(g, "manage_case_flow", retry))
    assert saved["state"] == "SUCCEEDED" and saved["retry_of_plan_id"] == failed["plan_id"]
    assert len(calls) == 2


def stepwise(g):
    g.stage.execution_mode = "STEPWISE"
    g.execution.run_mode = "MANUAL"
    g.execution.state = "ACTIVE"
    g.execution.parent_flow_run_id = g.template.id
    g.execution.automation_plan_json = {
        "start_node_key": "first",
        "node_drafts": {
            "first": {
                "row_version": 1,
                "startup_mode": "PROMPT",
                "startup_prompt": "stepwise",
                "agent_preset": g.preset,
                "input_bindings": {},
                "gates": [],
            }
        },
    }
    g.db.commit()


def test_real_stepwise_configuration_and_started_node_protection(flow_case):
    g = flow_case
    stepwise(g)
    draft = {
        "expected_row_version": 1,
        "startup_prompt": "more complete",
        "agent_preset": g.preset,
        "input_urls": {"link": "https://example.test/input"},
    }
    args = action(g, "CONFIGURE_EXECUTION", flow_node_key="first", stepwise_draft=draft)
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "SUCCEEDED", saved
    drafts = projection(rpc(g, "read_case_flow", {"section": "drafts"}))["items"]
    assert drafts[0]["row_version"] == 2 and drafts[0]["startup_prompt"] == "more complete"
    assert drafts[0]["input_bindings"]["link"]["artifact_version_id"]
    refresh(g)
    g.db.add(NodeRun(flow_run_id=g.execution.id, flow_node_snapshot_key="first", sequence_no=1))
    g.db.commit()
    draft["expected_row_version"] = 2
    blocked = projection(
        rpc(
            g,
            "manage_case_flow",
            action(g, "CONFIGURE_EXECUTION", flow_node_key="first", stepwise_draft=draft),
        )
    )
    assert blocked["state"] == "FAILED"
    refresh(g)
    assert (
        g.execution.automation_plan_json["node_drafts"]["first"]["startup_prompt"]
        == "more complete"
    )


def test_stepwise_start_passes_frozen_draft_and_native_binding_result(flow_case, monkeypatch):
    g = flow_case
    stepwise(g)
    calls = []

    def start(db, run_id, node_key, payload, command):
        calls.append((run_id, node_key, payload.expected_row_version, command))
        return {"binding_id": str(uuid4()), "id": str(uuid4())}

    monkeypatch.setattr(service.orchestration, "start_stepwise_node_draft", start)
    args = action(g, "START_STEPWISE_NODE", flow_node_key="first", draft_row_version=1)
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "SUCCEEDED", saved
    assert projection(rpc(g, "manage_case_flow", args)) == saved
    assert len(calls) == 1 and calls[0][:3] == (g.execution.id, "first", 1)
    assert "case-plan-stepwise-start:" in calls[0][3]


@pytest.mark.parametrize("state", ["PAUSED", "COMPLETED", "ARCHIVED"])
def test_pause_and_terminal_scope_fail_closed(flow_case, state):
    g = flow_case
    args = action(g)
    g.item.state = state
    g.db.commit()
    response = rpc(g, "manage_case_flow", args)
    if state == "PAUSED":
        assert projection(response)["error_code"] == "CASE_PAUSED"
        assert projection(rpc(g, "read_case_flow"))["flow_run"]["id"] == g.execution.id
    else:
        assert response.status_code == 401


def test_running_automatic_copy_cannot_be_edited(flow_case):
    g = flow_case
    g.execution.state = "ACTIVE"
    g.db.commit()
    plans = copy.deepcopy(g.execution.automation_plan_json["node_plans"])
    args = action(
        g,
        "CONFIGURE_EXECUTION",
        automatic_draft={"expected_row_version": 1, "start_node_key": "first", "node_plans": plans},
    )
    assert projection(rpc(g, "manage_case_flow", args))["state"] == "FAILED"
    refresh(g)
    assert g.execution.automation_plan_json["node_plans"] == plans


def test_partial_configuration_failure_rolls_back_inputs_but_preserves_audit(
    flow_case, monkeypatch
):
    g = flow_case

    def invalid(db, run_id, payload):
        db.get(FlowRun, run_id).name = "must roll back"
        db.flush()
        raise DomainError("INVALID_INPUT", "invalid input", 422)

    monkeypatch.setattr(service.orchestration, "update_automatic_run_draft", invalid)
    args = action(
        g,
        "CONFIGURE_EXECUTION",
        automatic_draft={"expected_row_version": 1, "start_node_key": "first", "node_plans": {}},
    )
    saved = projection(rpc(g, "manage_case_flow", args))
    assert saved["state"] == "FAILED" and saved["error_code"] == "INVALID_INPUT"
    refresh(g)
    assert g.execution.name == "execution"
    assert g.db.get(CaseControlPlan, saved["plan_id"]).state == "FAILED"


def test_flow_grant_rls_and_revocation(flow_case, db_session_factory):
    g = flow_case
    from flowweave.shared.secret_redaction import redact_secret_text

    assert g.flow_token not in redact_secret_text("failure " + g.flow_token)
    with tenant_user(str(uuid4())), db_session_factory() as other:
        assert other.scalars(select(CaseFlowToolGrant)).all() == []
    g.db.delete(g.binding)
    g.db.commit()
    assert rpc(g, "read_case_flow").status_code == 401
    assert g.db.scalar(select(CaseFlowToolGrant.id)) is None


@pytest.mark.parametrize("operation", ["copy", "configure"])
def test_locked_sdk_executes_copy_and_restores_same_action_without_duplicate(
    flow_case, tmp_path, operation
):
    from test_case_native_tools import run_locked_case_probe

    g = flow_case
    g.stage.current_execution_flow_run_id = None
    g.db.commit()
    if operation == "copy":
        args = action(g, "CREATE_EXECUTION_COPY")
    else:
        g.stage.current_execution_flow_run_id = g.execution.id
        g.db.commit()
        args = action(
            g,
            "CONFIGURE_EXECUTION",
            automatic_draft={
                "expected_row_version": 1,
                "start_node_key": "first",
                "node_plans": copy.deepcopy(g.execution.automation_plan_json["node_plans"]),
            },
        )
    run_locked_case_probe(g, tmp_path, native_tools.FLOWS, action_args=args)
    refresh(g)
    assert len(g.db.scalars(select(FlowRun)).all()) == (3 if operation == "copy" else 2)
    if operation == "configure":
        assert g.execution.row_version == 2
    plans = g.db.scalars(select(CaseControlPlan)).all()
    assert len(plans) == 1 and plans[0].state == "SUCCEEDED"


def test_concurrent_native_copy_requests_create_one_execution(flow_case):
    from concurrent.futures import ThreadPoolExecutor

    g = flow_case
    g.stage.current_execution_flow_run_id = None
    g.db.commit()
    args = action(g, "CREATE_EXECUTION_COPY")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: projection(rpc(g, "manage_case_flow", args)), range(2)))
    assert results[0] == results[1] and results[0]["state"] == "SUCCEEDED"
    refresh(g)
    assert len(g.db.scalars(select(FlowRun)).all()) == 3
    assert len(g.db.scalars(select(CaseControlPlan)).all()) == 1


@pytest.mark.parametrize("drift", ["round", "runtime", "frozen_version", "directory"])
def test_flow_grant_revalidates_current_coordinator(flow_case, drift):
    from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationCapability

    g = flow_case
    args = action(g)
    if drift == "round":
        g.item.current_control_round_id = str(uuid4())
    elif drift == "runtime":
        g.binding.runtime_session_id = str(uuid4())
    elif drift == "directory":
        g.binding.work_directory_version_id = str(uuid4())
    else:
        reference = g.db.scalar(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == g.binding.id,
                AgentConversationCapability.capability_key == native_tools.FLOWS.server_name,
            )
        )
        reference.digest = "0" * 64
    g.db.commit()
    assert rpc(g, "read_case_flow").status_code == 401
    assert rpc(g, "manage_case_flow", args).status_code == 401


def test_savepoint_compensates_only_failed_action_and_retains_outer_audit(flow_case, monkeypatch):
    from flowweave.shared.application.transactions import (
        register_commit_action,
        register_rollback_action,
    )

    g = flow_case
    calls = []

    def invalid(db, *args):
        register_commit_action(db, lambda: calls.append("bad commit"))
        register_rollback_action(db, lambda: calls.append("compensated"))
        db.get(FlowRun, g.execution.id).name = "discard this"
        db.flush()
        raise DomainError("CONFIGURATION_INVALID", "failure", 422)

    monkeypatch.setattr(service.orchestration, "update_automatic_run_draft", invalid)
    args = action(
        g,
        "CONFIGURE_EXECUTION",
        automatic_draft={"expected_row_version": 1, "start_node_key": "first", "node_plans": {}},
    )
    assert projection(rpc(g, "manage_case_flow", args))["state"] == "FAILED"
    assert calls == ["compensated"]
    refresh(g)
    assert g.execution.name == "execution"


def test_migration_enforces_flow_grant_rls_and_cascade(flow_case):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    source = Path(__file__).parents[1] / "migrations/versions/0182_case_flow_tools.py"
    spec = spec_from_file_location("case_flow_tool_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "case_flow_migration_" + uuid4().hex
    role = "case_flow_rls_" + uuid4().hex
    with flow_case.db.bind.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(text("CREATE TABLE case_control_rounds (id varchar(36) PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE agent_conversation_bindings (id varchar(36) PRIMARY KEY)")
        )
        connection.execute(
            text(
                "CREATE TABLE case_control_plans (id varchar(36) PRIMARY KEY, "
                "action varchar(40), CONSTRAINT ck_case_control_plan_action "
                "CHECK (action = 'START_EXECUTION'))"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(text("INSERT INTO case_control_rounds VALUES ('round')"))
        connection.execute(text("INSERT INTO agent_conversation_bindings VALUES ('binding')"))
        connection.execute(
            text("""INSERT INTO case_flow_tool_grants VALUES (
            'grant','owner','round','binding','directory','runtime','conversation',
            :digest, :encrypted, 'version', :digest, '{}', now())"""),
            {"digest": "1" * 64, "encrypted": b"encrypted-test-credential"},
        )
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, UPDATE ON case_flow_tool_grants TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_flow_tool_grants")) == 0
        assert (
            connection.execute(
                text("UPDATE case_flow_tool_grants SET token_digest = 'bad'")
            ).rowcount
            == 0
        )
        connection.execute(text("SELECT set_config('flowweave.user_id', 'owner', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_flow_tool_grants")) == 1
        connection.execute(text("RESET ROLE"))
        connection.execute(text("DELETE FROM case_control_rounds WHERE id = 'round'"))
        assert connection.scalar(text("SELECT count(*) FROM case_flow_tool_grants")) == 0
        assert connection.scalar(text("SELECT count(*) FROM agent_conversation_bindings")) == 1
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM agent_conversation_bindings")) == 1
        connection.rollback()


def test_pending_action_from_replaced_coordinator_cannot_be_executed_via_api(
    flow_case, monkeypatch
):
    from flowweave.shared.schemas import CaseControlPlanWrite

    g = flow_case
    args = action(g)
    requested = {
        key: value
        for key, value in args.items()
        if key not in {"action", "idempotency_key", "reason"}
    }
    plan = service.create_case_control_plan_for_binding(
        g.db,
        g.binding.id,
        CaseControlPlanWrite(
            stage_id=g.stage.id,
            action="START_EXECUTION",
            idempotency_key=args["idempotency_key"],
            reason=args["reason"],
            input_snapshot=requested,
        ),
    )
    g.item.current_control_round_id = str(uuid4())
    g.db.commit()
    calls = []
    monkeypatch.setattr(
        service.orchestration, "start_automatic_run", lambda *args: calls.append(args)
    )
    response = g.client.post(f"/api/v1/cases/{g.item.id}/control-plans/{plan['id']}/execute")
    assert response.status_code == 401
    refresh(g)
    assert g.db.get(CaseControlPlan, plan["id"]).state == "FAILED"
    assert not calls


@pytest.mark.parametrize("kind", ["foreign_artifact", "missing_model"])
def test_configuration_uses_existing_input_and_model_validation(flow_case, kind):
    g = flow_case
    plans = copy.deepcopy(g.execution.automation_plan_json["node_plans"])
    if kind == "foreign_artifact":
        plans["first"]["artifact_ids"] = {"link": str(uuid4())}
        plans["first"]["input_urls"] = {}
    else:
        plans["first"]["agent_preset"]["model_provider_id"] = str(uuid4())
    args = action(
        g,
        "CONFIGURE_EXECUTION",
        automatic_draft={"expected_row_version": 1, "start_node_key": "first", "node_plans": plans},
    )
    result = projection(rpc(g, "manage_case_flow", args))
    assert result["state"] == "FAILED" and result["error_code"]
    refresh(g)
    assert g.execution.row_version == 1
    assert (
        g.execution.automation_plan_json["node_plans"]["first"]["startup_prompt"]
        == "original first"
    )


def test_flow_tool_body_and_projection_bounds(flow_case):
    g = flow_case
    response = g.client.post(
        native_tools.FLOWS.path,
        headers={"Authorization": "Bearer " + g.flow_token},
        content=b"x" * 262_145,
    )
    assert response.json()["error"]["code"] == -32600
    plan = copy.deepcopy(g.execution.automation_plan_json)
    plan["node_plans"]["first"]["startup_prompt"] = "x" * 1_100_000
    g.execution.automation_plan_json = plan
    g.db.commit()
    response = rpc(g, "read_case_flow", {"section": "drafts", "limit": 1})
    result = projection(response)
    assert result["error_code"] == "CASE_FLOW_RESPONSE_TOO_LARGE"
    assert response.json()["result"]["isError"] is True
    assert (
        "original second"
        == projection(rpc(g, "read_case_flow", {"section": "drafts", "flow_node_key": "second"}))[
            "items"
        ][0]["startup_prompt"]
    )
