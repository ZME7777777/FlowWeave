from types import SimpleNamespace

import pytest

from flowweave.modules.gates.public import GateResult
from flowweave.modules.orchestration.application import service


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


@pytest.mark.parametrize("candidate", [None, SimpleNamespace(status="GATE_FAILED")])
@pytest.mark.parametrize("run_mode", ["MANUAL", "AUTOMATIC"])
def test_transitions_reject_outputs_without_a_passing_gate(monkeypatch, candidate, run_mode):
    attempt = SimpleNamespace(id="attempt-1")
    run = SimpleNamespace(automation_plan_json={})
    accepted = SimpleNamespace(
        accepted_attempt_id=attempt.id,
        flow_node_snapshot_key="source",
    )
    snapshot = SimpleNamespace(definition_json={"edges": []})

    monkeypatch.setattr(service, "_active_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(service, "_attempt", lambda *_args: attempt)
    monkeypatch.setattr(service, "_current_candidate_output_set", lambda *_args: candidate)

    with pytest.raises(service.DomainError) as error:
        if run_mode == "MANUAL":
            service._create_configurable_targets(SimpleNamespace(), run, accepted)
        else:
            service._advance_automatic_targets(SimpleNamespace(), run, accepted, [])

    assert error.value.code == "ATTEMPT_OUTPUT_NOT_ACCEPTED"


def test_transition_outputs_use_artifacts_from_passing_candidate(monkeypatch):
    candidate = SimpleNamespace(status="GATE_PASSED")
    artifacts = [SimpleNamespace(id="artifact-1", field_key="result")]

    monkeypatch.setattr(service, "_current_candidate_output_set", lambda *_args: candidate)
    monkeypatch.setattr(service, "_candidate_artifacts", lambda *_args: artifacts)

    assert service._accepted_transition_outputs(SimpleNamespace(), SimpleNamespace()) == artifacts
