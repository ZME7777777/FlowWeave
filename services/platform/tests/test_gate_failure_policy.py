"""Author-declared gate failure handling: validation, resolution and rules."""

import pytest
from pydantic import ValidationError

from flowweave.shared.domain.gate_failure_policy import (
    parse_on_fail,
    resolve,
    validate_on_fail,
)
from flowweave.shared.schemas import GateWrite

PRESET = {"model_provider_id": "p", "model_name": "m"}


def gate(stage="END", **config):
    return GateWrite(
        stage=stage,
        position=1,
        config={"prompt": "check", **config},
        agent_preset=PRESET,
    )


@pytest.mark.parametrize(
    "value,ok",
    [
        (None, True),
        ({"action": "ESCALATE"}, True),
        ({"action": "STOP", "note": "premise invalid"}, True),
        ({"action": "RERUN"}, True),
        ({"action": "RERUN", "max_rounds": 3}, True),
        ({"action": "RERUN", "max_rounds": 0}, False),
        ({"action": "RERUN", "max_rounds": 4}, False),
        ({"action": "RERUN", "max_rounds": True}, False),
        ({"action": "STOP", "max_rounds": 2}, False),
        ({"action": "FORK"}, False),
        ({"action": "RERUN", "extra": 1}, False),
        ({"action": "STOP", "note": "x" * 501}, False),
        ("STOP", False),
    ],
)
def test_validation(value, ok):
    assert (validate_on_fail(value) is None) is ok


def test_gate_schema_accepts_valid_and_rejects_invalid_or_start_stage():
    assert gate(on_fail={"action": "RERUN", "max_rounds": 2}).config["on_fail"]["max_rounds"] == 2
    with pytest.raises(ValidationError):
        gate(on_fail={"action": "RERUN", "max_rounds": 9})
    with pytest.raises(ValidationError):
        gate(stage="START", on_fail={"action": "STOP"})


def test_absent_or_invalid_declaration_means_historical_default():
    assert parse_on_fail({"id": "g", "config": {}}) is None
    assert parse_on_fail({"id": "g", "config": {"on_fail": {"action": "NOPE"}}}) is None
    assert parse_on_fail({"id": "g"}) is None


def test_defaults_and_serialisation():
    rerun = parse_on_fail({"id": "g", "config": {"on_fail": {"action": "RERUN"}}})
    assert rerun and rerun.max_rounds == 1 and rerun.as_dict()["max_rounds"] == 1
    stop = parse_on_fail({"id": "g", "config": {"on_fail": {"action": "STOP"}}})
    assert stop and stop.as_dict()["max_rounds"] is None


def test_resolution_only_considers_failed_gates_and_prefers_the_strictest():
    policies = [
        {"id": "a", "config": {"on_fail": {"action": "RERUN", "max_rounds": 3}}},
        {"id": "b", "config": {"on_fail": {"action": "STOP"}}},
        {"id": "c", "config": {"on_fail": {"action": "ESCALATE"}}},
        {"id": "d", "config": {}},
    ]
    assert resolve(policies, ["a"]).action == "RERUN"
    assert resolve(policies, ["a", "b"]).action == "STOP"
    assert resolve(policies, ["a", "c"]).action == "RERUN"
    assert resolve(policies, ["d"]) is None
    assert resolve(policies, []) is None
    tighter = [
        {"id": "x", "config": {"on_fail": {"action": "RERUN", "max_rounds": 3}}},
        {"id": "y", "config": {"on_fail": {"action": "RERUN", "max_rounds": 1}}},
    ]
    assert resolve(tighter, ["x", "y"]).max_rounds == 1
