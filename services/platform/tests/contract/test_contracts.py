from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from flowweave.bootstrap.api import create_app
from flowweave.modules.gates.application.executor import execute_gate
from flowweave.modules.orchestration.application.service import events
from flowweave.runtime.base import StartAttemptRequest
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.models import FlowDefinition, FlowRun, RunEvent

ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "contracts"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text())


def _validate(name: str, instance: object) -> None:
    validator = Draft202012Validator(_schema(name), format_checker=FormatChecker())
    validator.validate(instance)


def test_generated_openapi_matches_v1_baseline() -> None:
    expected = json.loads((CONTRACTS / "openapi-v1.json").read_text())
    assert create_app().openapi() == expected
    assert (
        all(path.startswith("/api/v1") for path in expected["paths"] if path != "/health") is False
    )
    assert "/api/v1/flow-runs/{run_id}/event-history" in expected["paths"]


def test_persisted_run_event_matches_schema_and_rejects_drift(db_session_factory) -> None:
    with db_session_factory() as db:
        flow = FlowDefinition(
            name="contract-flow",
            lark_root_folder_url="https://example.feishu.cn/drive/folder/contract-root",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="contract-run",
            lark_folder_token="contract-run-folder",
            lark_folder_url=("https://example.feishu.cn/drive/folder/contract-run-folder"),
        )
        db.add(run)
        db.flush()
        db.add(
            RunEvent(
                flow_run_id=run.id,
                event_type="CONTRACT_PROBE",
                payload_json={"ok": True},
            )
        )
        db.commit()
        value = events(db, run.id)[0]

    _validate("run-event.schema.json", value)
    invalid = deepcopy(value)
    invalid["type"] = invalid.pop("event_type")
    with pytest.raises(ValidationError):
        _validate("run-event.schema.json", invalid)


def test_runtime_adapter_result_matches_schema_and_rejects_unknown_status() -> None:
    runtime = MockRuntime()
    handle = runtime.start(
        StartAttemptRequest(
            attempt_id="attempt-contract",
            execution_key="contract:runtime:start",
            node={
                "alias": "Contract Node",
                "asset": {
                    "name": "Contract Asset",
                    "outputs": [{"field_key": "result", "data_type": "URL"}],
                },
            },
            bindings=[],
            workspace_ref="workspace/attempt-contract",
        )
    )
    value = runtime.inspect(handle).as_dict()
    _validate("runtime-result.schema.json", value)
    invalid = {**value, "status": "MYSTERY"}
    with pytest.raises(ValidationError):
        _validate("runtime-result.schema.json", invalid)


def test_gate_result_and_sandbox_input_match_schemas(db_session_factory) -> None:
    gate_input = {
        "schema_version": 1,
        "stage": "START",
        "attempt": {"id": "attempt-contract", "attempt_no": 1},
        "node": {
            "instance_key": "design",
            "alias": "Design",
            "asset_name": "Design Asset",
            "inputs": [],
            "outputs": [],
        },
        "input_bindings": [],
        "outputs": [],
        "artifacts": [],
    }
    _validate("gate-input.schema.json", gate_input)
    with db_session_factory() as db:
        result = execute_gate(
            db,
            "JAVASCRIPT",
            {
                "code": (
                    "return {decision: 'PASS', summary: 'contract', reasons: [], "
                    "evidence: [], details: {}};"
                )
            },
            gate_input,
            1,
        ).as_dict()
    _validate("gate-result.schema.json", result)

    invalid_input = {**gate_input, "stage": "MIDDLE"}
    invalid_result = {**result, "decision": "MAYBE"}
    with pytest.raises(ValidationError):
        _validate("gate-input.schema.json", invalid_input)
    with pytest.raises(ValidationError):
        _validate("gate-result.schema.json", invalid_result)
