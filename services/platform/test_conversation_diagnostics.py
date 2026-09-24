from __future__ import annotations

import json
import logging

import pytest

from flowweave.modules.agent_sessions.application import conversation_diagnostics
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeInputReadiness,
    RuntimeResult,
)


def test_logs_lifecycle_metadata_without_content(caplog: pytest.LogCaptureFixture) -> None:
    conversation_diagnostics._snapshots.clear()
    caplog.set_level(logging.INFO, logger="flowweave.agent_conversation_diagnostics")
    batch = RuntimeEventBatch(
        events=(
            RuntimeEvent("user-event", "MESSAGE", {"source": "user", "content": "private user"}),
            RuntimeEvent("tool-event", "OBSERVATION", {"content": "private tool output"}),
            RuntimeEvent(
                "assistant-event",
                "MESSAGE",
                {"source": "agent", "content": "private final reply"},
            ),
        ),
        cursor="assistant-event",
        result=RuntimeResult(
            status="COMPLETED",
            completion_event_id="assistant-event",
            completion_event_kind="ASSISTANT_MESSAGE",
        ),
        readiness=RuntimeInputReadiness(ready=True, execution_status="idle"),
    )

    conversation_diagnostics.log_conversation_diagnostic(
        operation="events",
        host_kind="agent_workspace",
        binding_id="binding-123",
        workspace_id="workspace-123",
        batch=batch,
    )

    record = next(
        record
        for record in caplog.records
        if record.name == "flowweave.agent_conversation_diagnostics"
    )
    fields = json.loads(record.getMessage().removeprefix("agent_conversation_diagnostic "))
    assert fields == {
        "assistant_message_count": 1,
        "attempt_id": None,
        "binding_id": "binding-123",
        "completion_event_id": "assistant-event",
        "completion_event_kind": "ASSISTANT_MESSAGE",
        "error_kind": None,
        "event_count": 3,
        "event_types": {"MESSAGE": 2, "OBSERVATION": 1},
        "execution_status": "idle",
        "flow_run_id": None,
        "history_cursor": None,
        "host_kind": "agent_workspace",
        "latest_assistant_message_id": "assistant-event",
        "latest_event_id": "assistant-event",
        "latest_event_type": "MESSAGE",
        "latest_user_message_id": "user-event",
        "next_cursor": "assistant-event",
        "operation": "events",
        "outcome": "ok",
        "trigger": None,
        "ready": True,
        "request_cursor": None,
        "request_history_cursor": None,
        "request_mode": "latest",
        "result_status": "COMPLETED",
        "user_message_count": 1,
        "workspace_id": "workspace-123",
    }
    assert "private user" not in record.getMessage()
    assert "private tool output" not in record.getMessage()
    assert "private final reply" not in record.getMessage()


def test_deduplicates_unchanged_polling(caplog: pytest.LogCaptureFixture) -> None:
    conversation_diagnostics._snapshots.clear()
    caplog.set_level(logging.INFO, logger="flowweave.agent_conversation_diagnostics")
    readiness = RuntimeInputReadiness(ready=False, execution_status="running")

    for _ in range(2):
        conversation_diagnostics.log_conversation_diagnostic(
            operation="input_readiness",
            host_kind="flow_node",
            binding_id="binding-456",
            flow_run_id="run-456",
            attempt_id="attempt-456",
            readiness=readiness,
        )

    records = [
        record
        for record in caplog.records
        if record.name == "flowweave.agent_conversation_diagnostics"
    ]
    assert len(records) == 1
    assert '"binding_id":"binding-456"' in records[0].getMessage()
    assert '"execution_status":"running"' in records[0].getMessage()
