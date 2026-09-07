import pytest

from flowweave.shared.schemas import EventTriggerWrite


def test_event_trigger_schema_accepts_governed_actions_and_filters():
    payload = EventTriggerWrite(
        trigger_key="runtime.failure",
        name="Runtime failure handler",
        event_types=["ERROR"],
        sources=["OPENHANDS"],
        failure_classes=["TRANSIENT_TIMEOUT"],
        actions=[
            {
                "action_type": "WEBHOOK",
                "config": {"url": "https://example.invalid/event"},
            },
            {"action_type": "NOTIFY", "config": {"target_ref": "ops-oncall"}},
        ],
    )
    assert payload.trigger_key == "runtime.failure"
    assert [action.action_type for action in payload.actions] == [
        "WEBHOOK",
        "NOTIFY",
    ]


def test_event_trigger_schema_rejects_duplicate_filters_and_secret_config():
    with pytest.raises(ValueError, match="must be unique"):
        EventTriggerWrite(
            trigger_key="runtime.failure",
            name="duplicate",
            event_types=["ERROR", "ERROR"],
            actions=[{"action_type": "NOTIFY"}],
        )
    with pytest.raises(ValueError, match="cannot contain secrets"):
        EventTriggerWrite(
            trigger_key="runtime.failure",
            name="secret",
            actions=[{"action_type": "WEBHOOK", "config": {"token": "value"}}],
        )


def test_event_trigger_schema_requires_lowercase_key_and_uppercase_event_type():
    with pytest.raises(ValueError):
        EventTriggerWrite(
            trigger_key="Runtime.Failure",
            name="bad key",
            actions=[{"action_type": "NOTIFY"}],
        )
    with pytest.raises(ValueError, match="uppercase"):
        EventTriggerWrite(
            trigger_key="runtime.failure",
            name="bad event",
            event_types=["error"],
            actions=[{"action_type": "NOTIFY"}],
        )
