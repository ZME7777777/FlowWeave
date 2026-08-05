from __future__ import annotations

from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.openhands import OpenHandsRuntime


def test_openhands_normalizes_incremental_events_and_terminal_result(settings, monkeypatch):
    runtime = OpenHandsRuntime(settings)
    responses = iter(
        [
            {
                "events": [
                    {
                        "type": "agent_message",
                        "cursor": "11",
                        "payload": {"content": "working"},
                    },
                    {
                        "kind": "tool_call",
                        "id": "12",
                        "data": {"tool": "search"},
                    },
                    {
                        "type": "future_event",
                        "sequence": 13,
                        "payload": {"raw": True},
                    },
                ],
                "next_cursor": "13",
                "status": "RUNNING",
            },
            {
                "items": [
                    {
                        "event_type": "completed",
                        "cursor": "14",
                        "payload": {"reason": "done"},
                    }
                ],
                "cursor": "14",
                "status": "COMPLETED",
                "outputs": {"design": {"artifact_type": "DOCUMENT", "content": "final design"}},
            },
        ]
    )
    requests: list[tuple[str, str, object]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append((method, path, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr(runtime, "_request", fake_request)
    handle = RuntimeHandle("job-1", "conversation-1", "10")

    running = runtime.read_events(handle)
    terminal = runtime.read_events(RuntimeHandle("job-1", "conversation-1", running.cursor))

    assert [event.event_type for event in running.events] == ["MESSAGE", "TOOL", "UNKNOWN"]
    assert [event.cursor for event in running.events] == ["11", "12", "13"]
    assert running.events[2].payload["source_type"] == "FUTURE_EVENT"
    assert running.cursor == "13"
    assert running.result is None
    assert terminal.cursor == "14"
    assert terminal.events[0].event_type == "COMPLETED"
    assert terminal.result is not None
    assert terminal.result.status == "COMPLETED"
    assert terminal.result.outputs == {"design": ("DOCUMENT", "final design")}
    assert requests == [
        ("GET", "/api/conversations/conversation-1/events", {"after": "10"}),
        ("GET", "/api/conversations/conversation-1/events", {"after": "13"}),
    ]
