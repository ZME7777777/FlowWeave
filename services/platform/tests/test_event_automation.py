import pytest

from flowweave.shared.domain.event_automation import (
    ActionType,
    EventSource,
    EventTrigger,
    FailureClass,
    TriggerAction,
    TriggerEvent,
    TriggerFilter,
    action_idempotency_key,
    classify_failure,
    is_auto_recovery_eligible,
)


def _event(**changes: object) -> TriggerEvent:
    values: dict[str, object] = {
        "event_id": "openhands-event-1",
        "event_type": "ERROR",
        "source": EventSource.OPENHANDS,
        "flow_run_id": "run-1",
        "node_run_id": "node-1",
        "attempt_id": "attempt-1",
        "conversation_id": "conversation-1",
        "action_id": "action-1",
        "tool_call_id": "tool-call-1",
        "failure_class": FailureClass.TRANSIENT_NETWORK,
    }
    values.update(changes)
    return TriggerEvent(**values)


def test_filter_matches_formal_identity_and_failure_class():
    trigger = EventTrigger(
        trigger_id="trigger-1",
        version=2,
        event_filter=TriggerFilter(
            event_types=frozenset({"ERROR"}),
            failure_classes=frozenset({FailureClass.TRANSIENT_NETWORK}),
            node_run_ids=frozenset({"node-1"}),
        ),
        actions=(TriggerAction(ActionType.RESUME_CONVERSATION),),
    )

    assert trigger.matches(_event())
    assert not trigger.matches(_event(failure_class=FailureClass.AUTHENTICATION))
    assert not trigger.matches(_event(node_run_id="node-2"))


def test_failure_classification_is_conservative_and_recovery_is_transient_only():
    assert classify_failure("LLMRateLimitError", "too many requests") == FailureClass.QUOTA
    assert classify_failure("ReadTimeout", "upstream timed out") == FailureClass.TRANSIENT_TIMEOUT
    assert classify_failure("BadRequest", "invalid request") == FailureClass.INVALID_REQUEST
    assert classify_failure("Unexpected", "something happened") == FailureClass.UNKNOWN
    assert is_auto_recovery_eligible(FailureClass.TRANSIENT_NETWORK)
    assert not is_auto_recovery_eligible(FailureClass.QUOTA)
    assert not is_auto_recovery_eligible(FailureClass.UNKNOWN)


def test_action_idempotency_key_is_stable_per_event_and_action_position():
    trigger = EventTrigger(
        trigger_id="trigger-1",
        version=1,
        event_filter=TriggerFilter(),
        actions=(
            TriggerAction(ActionType.NOTIFY),
            TriggerAction(ActionType.CREATE_TASK),
        ),
    )
    first = action_idempotency_key(trigger, _event(), 0)
    assert first == action_idempotency_key(trigger, _event(), 0)
    assert first != action_idempotency_key(trigger, _event(), 1)
    assert first != action_idempotency_key(trigger, _event(event_id="openhands-event-2"), 0)


def test_trigger_and_event_reject_blank_identity():
    with pytest.raises(ValueError, match="event_id"):
        _event(event_id=" ")
    with pytest.raises(ValueError, match="at least one action"):
        EventTrigger("trigger-1", 1, TriggerFilter(), ())
