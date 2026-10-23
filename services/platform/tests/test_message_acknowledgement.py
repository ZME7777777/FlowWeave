"""Formal user-event identity and bootstrap ancestry regressions."""

from types import SimpleNamespace

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import conversations
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle
from flowweave.runtime.openhands import OpenHandsRuntime


@pytest.fixture(autouse=True)
def database():
    """These identity checks need no database or model."""


def test_success_only_append_acknowledges_first_and_subsequent_user_events(monkeypatch):
    runtime = OpenHandsRuntime(Settings())
    handle = RuntimeHandle("agent-workspace:test", "conversation")
    events = []
    posts = []

    def request(method, path, **kwargs):
        if method == "GET":
            # Native EventService filters by the full Python class name,
            # although returned event JSON uses the short `kind` value.
            params = kwargs["params"]
            matches = (
                params["kind"] == ("openhands.sdk.event.llm_convertible.message.MessageEvent")
                and params["source"] == "user"
            )
            return {"items": list(events) if matches else []}
        assert method == "POST" and path.endswith("/events")
        posts.append(kwargs["json"])
        events.append({"id": f"user-{len(posts)}", "kind": "MessageEvent", "source": "user"})
        return {"success": True}

    monkeypatch.setattr(runtime, "_request", request)
    monkeypatch.setattr(runtime, "_base_url_for_handle", lambda _: "http://runtime.test")
    monkeypatch.setattr(runtime, "_session_key_for_handle", lambda _: "test")
    assert runtime.send_message(handle, "first").cursor == "user-1"
    assert runtime.send_message(handle, "continue").cursor == "user-2"
    assert len(posts) == 2


@pytest.mark.parametrize(
    ("events", "previous", "expected"),
    [
        ([("user", "MESSAGE", "user", "__root__")], None, "user"),
        ([("user", "MESSAGE", "user", None)], None, "user"),
        (
            [("system", "SystemPromptEvent", "agent", None), ("user", "MESSAGE", "user", "system")],
            None,
            "user",
        ),
        (
            [
                ("system", "SystemPromptEvent", "agent", None),
                ("user", "MESSAGE", "user", "system"),
                ("reply", "MESSAGE", "agent", "user"),
                ("later", "MESSAGE", "user", "reply"),
            ],
            None,
            "user",
        ),
        ([("user", "MESSAGE", "user", "known")], "known", "user"),
        ([("user", "MESSAGE", "user", "missing")], None, None),
        (
            [("state", "STATE", "environment", None), ("user", "MESSAGE", "user", "state")],
            None,
            None,
        ),
        (
            [
                ("system", "SystemPromptEvent", "agent", "system"),
                ("user", "MESSAGE", "user", "system"),
            ],
            None,
            None,
        ),
        (
            [("system", "SystemPromptEvent", "agent", None), ("user", "MESSAGE", "user", "system")],
            "other-parent",
            None,
        ),
        ([("a", "MESSAGE", "user", "__root__"), ("b", "MESSAGE", "user", "__root__")], None, None),
    ],
)
def test_bootstrap_recovers_only_the_unique_formal_initial_user_event(
    monkeypatch, events, previous, expected
):
    batch = RuntimeEventBatch(
        events=tuple(
            RuntimeEvent(
                cursor=identity,
                event_type=kind if kind == "MESSAGE" else "STATE",
                payload={"source": source, "parent_id": parent, "source_type": kind},
            )
            for identity, kind, source, parent in reversed(events)
        )
    )
    monkeypatch.setattr(
        conversations, "get_runtime", lambda: SimpleNamespace(read_active_events=lambda _: batch)
    )
    assert (
        conversations._initial_user_event_id(RuntimeHandle("job", "conversation"), previous)
        == expected
    )
