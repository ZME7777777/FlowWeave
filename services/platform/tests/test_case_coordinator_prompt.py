from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowweave.modules.cases.application import coordination
from flowweave.modules.cases.application.flow_tools import FlowActionArguments


def _prompt() -> str:
    return coordination._prompt({"name": "场景", "case_id": "case-1"}, "workspace-1", "round-1")


def test_prompt_maps_each_node_state_to_the_action_that_fits_it() -> None:
    text = _prompt()
    # A gate pass is auto-accepted, so review happens after ACCEPTED and rerun is the way back.
    assert "自动接受" in text
    assert "RERUN_STEPWISE_NODE" in text and "WAITING_ACCEPTANCE" in text
    assert "COPY_STEPWISE_EXECUTION" in text and "CANCEL_STEPWISE_ATTEMPT" in text
    # Everything the coordinator may change in a node draft.
    for field in ("startup_prompt", "input_urls", "capability_version_ids", "gates"):
        assert field in text


def _action(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "action": "RERUN_STEPWISE_NODE",
        "idempotency_key": "rerun-n2-1",
        "reason": "business goal unmet",
        "expected_case_row_version": 1,
        "expected_stage_key": "collect",
        "expected_execution_flow_run_id": "run-1",
        "expected_run_row_version": 1,
        "expected_snapshot_version": 1,
        "flow_node_key": "investigate",
        "node_run_id": "node-run-1",
    }
    base.update(overrides)
    return base


def test_rerun_accepts_only_node_identity() -> None:
    FlowActionArguments.model_validate(_action())
    for extra in ({"draft_row_version": 1}, {"node_attempt_id": "attempt-1"}):
        with pytest.raises(ValidationError) as error:
            FlowActionArguments.model_validate(_action(**extra))
        # The message tells the Agent which fields this action takes.
        assert "RERUN_STEPWISE_NODE requires" in str(error.value) or "forbidden" in str(error.value)
