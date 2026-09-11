from __future__ import annotations

import io
import logging

import pytest

from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.secret_redaction import (
    SecretRedactionFilter,
    redact_secret_text,
    redact_secret_value,
)


@pytest.fixture(autouse=True)
def database():
    """Secret-boundary checks do not require PostgreSQL or Docker."""

    yield


def _sentinel() -> str:
    return "sk-oh-" + "a" * 32


def test_redaction_removes_runtime_keys_and_nested_tool_literals() -> None:
    sentinel = _sentinel()
    value = redact_secret_value(
        {
            "OH_SECRET_KEY": "x" * 32,
            "nested": {
                "OH_SESSION_API_KEYS_1": "y" * 32,
                "observation": f"tool output token={sentinel}",
                "environment": {"UNRECOGNIZED_NAME": "must-not-leak"},
            },
        }
    )

    assert value == {
        "OH_SECRET_KEY": "[redacted]",
        "nested": {
            "OH_SESSION_API_KEYS_1": "[redacted]",
            "observation": "tool output token=[redacted]",
            "environment": "[redacted]",
        },
    }
    assert sentinel not in redact_secret_text(f"tmux command {sentinel}")


def test_openhands_event_projection_redacts_model_and_tool_observation_content() -> None:
    sentinel = _sentinel()
    message = OpenHandsRuntime._event_payload(
        {
            "kind": "MessageEvent",
            "llm_message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"model {sentinel}"}],
            },
        }
    )
    observation = OpenHandsRuntime._event_payload(
        {
            "kind": "ObservationEvent",
            "observation": {
                "kind": "TerminalObservation",
                "content": [{"type": "text", "text": f"result {sentinel}"}],
                "environment": {"SESSION_API_KEY": "z" * 32},
            },
        }
    )

    assert sentinel not in str(message)
    assert sentinel not in str(observation)
    assert message["content"] == "model [redacted]"
    assert observation["content"] == "result [redacted]"
    assert observation["details"]["environment"] == "[redacted]"


def test_runtime_provider_log_filter_redacts_libtmux_style_message() -> None:
    sentinel = _sentinel()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("flowweave.test.secret-redaction")
    original_handlers = logger.handlers[:]
    original_propagate = logger.propagate
    logger.handlers = [handler]
    logger.propagate = False
    try:
        logger.warning("libtmux send-keys %s", sentinel)
        output = stream.getvalue()
        assert sentinel not in output
        assert "[redacted]" in output
    finally:
        logger.handlers = original_handlers
        logger.propagate = original_propagate


def test_openhands_stream_and_terminal_result_do_not_bypass_redaction() -> None:
    sentinel = _sentinel()
    stream = OpenHandsRuntime._visible_stream_event(
        {"kind": "StreamingDeltaEvent", "content": f"delta {sentinel}"}
    )
    runtime = OpenHandsRuntime.__new__(OpenHandsRuntime)
    runtime._contracts = {}  # pyright: ignore[reportAttributeAccessIssue]
    result = OpenHandsRuntime._result_from_events(
        runtime,
        RuntimeHandle(job_id="test", conversation_id="conversation-1"),
        [
            {
                "kind": "ActionEvent",
                "id": "finish-1",
                "action": {"kind": "FinishAction", "message": f"final {sentinel}"},
            }
        ],
        "finish-1",
    )

    assert sentinel not in str(stream)
    assert stream == ({"type": "delta", "content": "delta [redacted]"},)
    assert result is not None
    assert result.final_message == "final [redacted]"
