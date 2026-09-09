from types import SimpleNamespace

from flowweave.modules.orchestration.application import service
from flowweave.modules.gates.public import GateResult


def test_candidate_artifacts_follow_frozen_ids_not_query_order(monkeypatch):
    first = SimpleNamespace(id="a-1", field_key="report")
    second = SimpleNamespace(id="a-2", field_key="report")
    candidate = SimpleNamespace(artifact_ids_json=["a-2", "a-1"])

    class Db:
        def scalars(self, _statement):
            return iter([first, second])

    result = service._candidate_artifacts(Db(), candidate)

    assert [item.id for item in result] == ["a-2", "a-1"]


def test_end_gate_business_failure_marks_candidate_failed(monkeypatch):
    candidate = SimpleNamespace(id="candidate-1", status="PENDING_REVIEW", gate_error_code=None)
    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-1",
        state_version=1,
        gate_policies_json=[],
        current_candidate_output_set_id=candidate.id,
    )
    node_run = SimpleNamespace(id="node-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="AUTOMATIC", state="ACTIVE")

    class Db:
        def add(self, _item):
            return None

    monkeypatch.setattr(service, "_current_candidate_output_set", lambda *_args: candidate)
    monkeypatch.setattr(service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(service, "_run", lambda *_args: run)
    monkeypatch.setattr(service, "_event", lambda *_args: None)

    prepared = SimpleNamespace(
        policy={"id": "gate-1", "position": 0},
        execution_no=1,
        plan=SimpleNamespace(sidecar_binding_id=None),
    )
    service._record_gate_results(
        Db(), attempt, "END", {}, [(prepared, GateResult("FAIL", "bad", [], [], {}))], "END_BLOCKED"
    )

    assert candidate.status == "GATE_FAILED"
    assert candidate.gate_error_code is None


def test_end_gate_execution_error_is_distinct_from_business_failure(monkeypatch):
    candidate = SimpleNamespace(id="candidate-1", status="PENDING_REVIEW", gate_error_code=None)
    attempt = SimpleNamespace(
        id="attempt-1",
        node_run_id="node-1",
        state_version=1,
        gate_policies_json=[],
        current_candidate_output_set_id=candidate.id,
    )
    node_run = SimpleNamespace(id="node-1", flow_run_id="run-1")
    run = SimpleNamespace(id="run-1", run_mode="MANUAL", state="ACTIVE")

    class Db:
        def add(self, _item):
            return None

    monkeypatch.setattr(service, "_current_candidate_output_set", lambda *_args: candidate)
    monkeypatch.setattr(service, "_node_run", lambda *_args: node_run)
    monkeypatch.setattr(service, "_run", lambda *_args: run)
    monkeypatch.setattr(service, "_event", lambda *_args: None)

    prepared = SimpleNamespace(
        policy={"id": "gate-1", "position": 0},
        execution_no=1,
        plan=SimpleNamespace(sidecar_binding_id=None),
    )
    result = GateResult("ERROR", "provider unavailable", [], [], {}, error_code="GATE_TIMEOUT")
    service._record_gate_results(Db(), attempt, "END", {}, [(prepared, result)], "END_BLOCKED")

    assert candidate.status == "GATE_ERROR"
    assert candidate.gate_error_code == "GATE_TIMEOUT"
