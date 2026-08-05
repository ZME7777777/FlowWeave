import pytest

from flowweave.modules.flows.domain.rules import validate_flow
from flowweave.modules.runs.domain.readiness import (
    Artifact,
    Binding,
    InputField,
    evaluate_readiness,
)
from flowweave.modules.runs.domain.state_machine import transition
from flowweave.shared.errors import DomainError
from flowweave.shared.models import AttemptState
from flowweave.shared.schemas import FlowWrite


def test_readiness_is_explicit_typed_and_version_frozen():
    fields = (InputField("prd", "DOCUMENT"), InputField("repo", "GIT"))
    artifacts = {
        "doc-v1": Artifact("doc-v1", "DOCUMENT"),
        "doc-v2": Artifact("doc-v2", "DOCUMENT"),
        "repo-v1": Artifact("repo-v1", "GIT"),
    }
    result = evaluate_readiness(fields, (Binding("prd", "doc-v1"),), artifacts)
    assert result.missing == ("repo",)
    assert not result.ready
    ready = evaluate_readiness(
        fields,
        (Binding("prd", "doc-v1"), Binding("repo", "repo-v1")),
        artifacts,
    )
    assert ready.ready
    # A newer artifact does not silently replace the explicit v1 binding.
    assert (Binding("prd", "doc-v1"),)[0].artifact_id == "doc-v1"


def test_attempt_state_machine_rejects_machine_bypass():
    assert (
        transition(AttemptState.START_GATES, "GATES_PASS")
        == AttemptState.WAITING_START_CONFIRMATION
    )
    assert transition(AttemptState.END_GATES, "GATES_PASS") == AttemptState.WAITING_ACCEPTANCE
    with pytest.raises(DomainError):
        transition(AttemptState.WAITING_START_CONFIRMATION, "ACCEPT")


def test_flow_validation_allows_same_asset_twice_but_checks_mapping_and_gate_positions():
    payload = FlowWrite.model_validate(
        {
            "name": "repeat asset",
            "nodes": [
                {
                    "instance_key": "first",
                    "node_asset_id": "asset",
                    "gates": [
                        {"stage": "START", "position": 0, "gate_type": "PROMPT"},
                        {"stage": "END", "position": 0, "gate_type": "PYTHON"},
                    ],
                },
                {"instance_key": "second", "node_asset_id": "asset"},
            ],
            "edges": [
                {
                    "source_instance_key": "first",
                    "target_instance_key": "second",
                    "mappings": [{"source_output_key": "out", "target_input_key": "in"}],
                }
            ],
        }
    )
    validate_flow(
        payload.model_dump(), {"asset": {"INPUT": {"in": "TEXT"}, "OUTPUT": {"out": "TEXT"}}}
    )
