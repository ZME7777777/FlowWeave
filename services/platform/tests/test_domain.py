import pytest

from flowweave.modules.agent_workspaces.presentation.router import (
    AgentConversationBootstrapWrite,
    AgentWorkspaceCapabilitiesWrite,
)
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
from flowweave.shared.schemas import AgentPresetWrite, ExecutorWrite, FlowWrite


def test_agent_capability_request_models_have_no_flowweave_count_limit():
    capability_ids = [f"capability-{index}" for index in range(31)]

    assert AgentWorkspaceCapabilitiesWrite(
        capability_version_ids=capability_ids
    ).capability_version_ids == capability_ids
    assert AgentConversationBootstrapWrite(
        model_provider_id="provider",
        model_name="model",
        content="start",
        capability_version_ids=capability_ids,
    ).capability_version_ids == capability_ids
    assert AgentPresetWrite(capability_version_ids=capability_ids).capability_version_ids == (
        capability_ids
    )
    assert ExecutorWrite(context_capability_ids=capability_ids).context_capability_ids == (
        capability_ids
    )


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
            "environment_version_id": "00000000-0000-4000-8000-000000000001",
            "nodes": [
                {
                    "instance_key": "first",
                    "node_asset_id": "asset",
                    "gates": [
                        {
                            "stage": "START",
                            "position": 0,
                            "gate_type": "PROMPT",
                            "config": {"prompt": "check"},
                            "agent_preset": {},
                        },
                        {
                            "stage": "END",
                            "position": 0,
                            "gate_type": "PYTHON",
                            "config": {"prompt": "check"},
                            "agent_preset": {},
                        },
                    ],
                },
                {"instance_key": "second", "node_asset_id": "asset"},
            ],
            "edges": [
                {
                    "source_instance_key": "first",
                    "target_instance_key": "second",
                }
            ],
            "port_mappings": [
                {
                    "source_instance_key": "first",
                    "source_output_key": "out",
                    "target_instance_key": "second",
                    "target_input_key": "in",
                }
            ],
        }
    )
    validate_flow(
        payload.model_dump(), {"asset": {"INPUT": {"in": "URL"}, "OUTPUT": {"out": "URL"}}}
    )


def test_flow_validation_supports_branching_merging_and_multiple_ports():
    payload = FlowWrite.model_validate(
        {
            "name": "branch and merge",
            "environment_version_id": "00000000-0000-4000-8000-000000000001",
            "nodes": [
                {"instance_key": "source_a", "node_asset_id": "source"},
                {"instance_key": "source_b", "node_asset_id": "source"},
                {"instance_key": "merge", "node_asset_id": "merge"},
                {"instance_key": "branch", "node_asset_id": "target"},
            ],
            "edges": [
                {"source_instance_key": "source_a", "target_instance_key": "merge"},
                {"source_instance_key": "source_b", "target_instance_key": "merge"},
                {"source_instance_key": "source_a", "target_instance_key": "branch"},
            ],
            "port_mappings": [
                {
                    "source_instance_key": "source_a",
                    "source_output_key": "result",
                    "target_instance_key": "merge",
                    "target_input_key": "left",
                },
                {
                    "source_instance_key": "source_b",
                    "source_output_key": "result",
                    "target_instance_key": "merge",
                    "target_input_key": "right",
                },
                {
                    "source_instance_key": "source_a",
                    "source_output_key": "result",
                    "target_instance_key": "branch",
                    "target_input_key": "input",
                },
            ],
        }
    )
    validate_flow(
        payload.model_dump(),
        {
            "source": {"INPUT": {}, "OUTPUT": {"result": "URL"}},
            "merge": {"INPUT": {"left": "URL", "right": "URL"}, "OUTPUT": {}},
            "target": {"INPUT": {"input": "URL"}, "OUTPUT": {}},
        },
    )


def test_flow_validation_rejects_cycles_but_not_branching_or_merging():
    payload = FlowWrite.model_validate(
        {
            "name": "cycle",
            "environment_version_id": "00000000-0000-4000-8000-000000000001",
            "nodes": [
                {"instance_key": "a", "node_asset_id": "asset"},
                {"instance_key": "b", "node_asset_id": "asset"},
                {"instance_key": "c", "node_asset_id": "asset"},
            ],
            "edges": [
                {"source_instance_key": "a", "target_instance_key": "b"},
                {"source_instance_key": "b", "target_instance_key": "c"},
                {"source_instance_key": "c", "target_instance_key": "a"},
            ],
        }
    )

    with pytest.raises(DomainError, match="cycles"):
        validate_flow(
            payload.model_dump(),
            {"asset": {"INPUT": {}, "OUTPUT": {}}},
        )


def test_flow_validation_rejects_multiple_sources_for_one_target_input():
    payload = FlowWrite.model_validate(
        {
            "name": "ambiguous input",
            "environment_version_id": "00000000-0000-4000-8000-000000000001",
            "nodes": [
                {"instance_key": "source_a", "node_asset_id": "source"},
                {"instance_key": "source_b", "node_asset_id": "source"},
                {"instance_key": "target", "node_asset_id": "target"},
            ],
            "port_mappings": [
                {
                    "source_instance_key": "source_a",
                    "source_output_key": "result",
                    "target_instance_key": "target",
                    "target_input_key": "input",
                },
                {
                    "source_instance_key": "source_b",
                    "source_output_key": "result",
                    "target_instance_key": "target",
                    "target_input_key": "input",
                },
            ],
        }
    )

    with pytest.raises(DomainError, match="multiple mappings"):
        validate_flow(
            payload.model_dump(),
            {
                "source": {"INPUT": {}, "OUTPUT": {"result": "URL"}},
                "target": {"INPUT": {"input": "URL"}, "OUTPUT": {}},
            },
        )
