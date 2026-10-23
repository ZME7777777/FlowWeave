from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from flowweave.modules.agent_sessions.application import conversations, history
from flowweave.modules.agent_sessions.presentation import history_mcp as mcp
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database():
    """Formal event slices and scope fences require no database server."""


def event(content, kind="MESSAGE", source="assistant", identity="formal-event"):
    return RuntimeEvent(
        cursor=identity, event_type=kind, payload={"source": source, "content": content}
    )


@pytest.mark.parametrize("size", [1, 10, 2_000, 10_000])
def test_long_visible_content_can_be_reassembled_without_loss(size):
    original = "开头🙂\n\n" + "有中文和 emoji🚀。" * 1_000 + "\n结尾"
    prompt, _ = conversations.message_payload(
        original, (), ({"event_id": "hidden", "content": "隐藏背景不能成为正文"},)
    )
    source = event(prompt)
    offset = 0
    chunks = []
    while True:
        part = history.message_content(source, content_offset=offset, max_content_chars=size)
        assert part["event_id"] == source.cursor
        assert part["content_chars"] == len(original)
        assert part["content_offset"] == offset
        chunks.append(part["content"])
        if part["next_content_offset"] is None:
            assert part["truncated"] is False
            break
        assert offset < part["next_content_offset"] <= len(original)
        offset = part["next_content_offset"]
    assert "".join(chunks) == original
    assert history.message_content(source, content_offset=len(original))["content"] == ""


@pytest.mark.parametrize(
    "source",
    [
        event("tool output", "TOOL_RESULT"),
        event("system", source="system"),
        event("", source="user"),
        event("bad role", source={}),
    ],
)
def test_content_read_never_returns_tool_system_or_invalid_messages(source):
    with pytest.raises(DomainError) as caught:
        history.message_content(source)
    assert caught.value.code == "AGENT_HISTORY_EVENT_UNAVAILABLE"


@pytest.mark.parametrize(
    "params",
    [
        {"content_offset": 1},
        {"event_id": "event", "query": "keyword"},
        {"event_id": "event", "history_cursor": "older"},
        {"event_id": "event", "content_offset": -1},
        {"event_id": "event", "content_offset": True},
    ],
)
def test_content_query_rejects_ambiguous_or_invalid_arguments(params):
    with pytest.raises(ValidationError):
        mcp.HistoryArguments(**params)


def test_content_read_uses_only_authorized_native_handle(monkeypatch):
    handle = RuntimeHandle(job_id="authorized", conversation_id="native-source")
    prepared = SimpleNamespace(locator=SimpleNamespace(handle=handle))
    page = mcp.AuthorizedPage("owner", "grant", prepared)
    calls = []

    class Runtime:
        def reload_conversation(self, actual):
            assert actual is handle
            calls.append("reload")

        def read_event(self, actual, identity):
            assert actual is handle
            assert identity == "formal-event"
            calls.append("read-event")
            return event("正式长消息")

        def read_active_events(self, *_args):
            raise AssertionError("Event content must not scan the history")

    monkeypatch.setattr(mcp, "get_runtime", lambda: Runtime())
    args = mcp.HistoryArguments(event_id="formal-event", content_offset=2)
    batch = mcp.read_page(page, args)
    assert calls == ["reload", "read-event"]
    assert batch.events[0].cursor == "formal-event"
    monkeypatch.setattr(
        mcp.history_access,
        "authorize",
        lambda *_args: SimpleNamespace(
            id="grant", owner_user_id="owner", source_binding_id="source"
        ),
    )
    monkeypatch.setattr(
        conversations, "assert_prepared_conversation_events_current", lambda *_args: None
    )
    result = mcp.project_page(None, page, batch, "fixture", args)
    assert result["source_binding_id"] == "source"
    assert result["message"]["content"] == "长消息"
    with pytest.raises(DomainError):
        mcp.project_page(
            None,
            page,
            RuntimeEventBatch(events=(event("other", identity="wrong"),)),
            "fixture",
            args,
        )
    with pytest.raises(DomainError):
        history.message_content(event("短"), content_offset=2)


@pytest.mark.parametrize("returned", [None, event("other", identity="wrong")])
def test_native_content_read_rejects_missing_or_different_event(monkeypatch, returned):
    handle = RuntimeHandle(job_id="authorized", conversation_id="native-source")
    page = mcp.AuthorizedPage(
        "owner", "grant", SimpleNamespace(locator=SimpleNamespace(handle=handle))
    )
    monkeypatch.setattr(
        mcp,
        "get_runtime",
        lambda: SimpleNamespace(
            reload_conversation=lambda _handle: None,
            read_event=lambda _handle, _identity: returned,
        ),
    )
    with pytest.raises(DomainError) as caught:
        mcp.read_page(page, mcp.HistoryArguments(event_id="formal-event"))
    assert caught.value.code == "AGENT_HISTORY_EVENT_UNAVAILABLE"


@pytest.mark.parametrize("denied_at", ["authorization", "generation"])
def test_content_projection_rechecks_access_after_native_read(monkeypatch, denied_at):
    def deny(*_args):
        raise DomainError("DENIED", "denied", 403)

    monkeypatch.setattr(
        mcp.history_access,
        "authorize",
        deny
        if denied_at == "authorization"
        else lambda *_args: SimpleNamespace(id="grant", owner_user_id="owner"),
    )
    monkeypatch.setattr(
        conversations,
        "assert_prepared_conversation_events_current",
        deny if denied_at == "generation" else lambda *_args: None,
    )
    with pytest.raises(DomainError) as caught:
        mcp.project_page(
            None,
            mcp.AuthorizedPage("owner", "grant", SimpleNamespace()),
            RuntimeEventBatch(events=(event("private text"),)),
            "fixture",
            mcp.HistoryArguments(event_id="formal-event"),
        )
    assert caught.value.code == "DENIED"
