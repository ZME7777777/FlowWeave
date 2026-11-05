from dataclasses import replace
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import conversations, history
from flowweave.modules.agent_workspaces.presentation import router
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database():
    """Controlled native pages and authorization callbacks need no database."""


def event(identity, source, content, event_type="MESSAGE"):
    return RuntimeEvent(
        cursor=identity, event_type=event_type, payload={"source": source, "content": content}
    )


def test_history_returns_visible_messages_with_formal_pagination():
    prompt, _ = conversations.message_payload(
        "可见提问", (), ({"event_id": "ref", "content": "隐藏引用正文"},)
    )
    batch = RuntimeEventBatch(
        events=(
            event("user", "user", prompt),
            event("tool", "agent", "敏感工具结果", "TOOL_RESULT"),
            event("system", "system", "系统内容"),
            event("reply", "assistant", "正式回复"),
        ),
        cursor="native-head",
        history_cursor="native-older-page",
    )
    page = history.message_page(batch)
    assert [(item["event_id"], item["content"]) for item in page["messages"]] == [
        ("user", "可见提问"),
        ("reply", "正式回复"),
    ]
    assert page["next_cursor"] == "native-head"
    assert page["history_cursor"] == "native-older-page"
    assert "隐藏引用正文" not in str(page)
    assert "敏感工具结果" not in str(page)
    filtered = history.message_page(batch, query="回复", max_content_chars=2)
    assert filtered["query_scope"] == "current_page"
    assert filtered["messages"] == [
        {
            "event_id": "reply",
            "source": "assistant",
            "content": "正式",
            "content_chars": 4,
            "truncated": True,
        }
    ]
    empty = history.message_page(batch, query="没有匹配")
    assert empty["messages"] == []
    assert empty["history_cursor"] == "native-older-page"
    assert history.message_page(replace(batch, history_cursor=None))["history_cursor"] is None


@pytest.mark.parametrize("limit", [0, -1, 12_001])
def test_history_rejects_invalid_content_limits(limit):
    with pytest.raises(DomainError, match="正文长度"):
        history.message_page(RuntimeEventBatch(events=()), max_content_chars=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["success", "prepare_denied", "project_denied"])
async def test_history_route_uses_phased_authorization_and_history_lane(monkeypatch, phase):
    calls = []
    prepared = object()
    batch = RuntimeEventBatch(events=(event("formal", "agent", "回复"),), cursor="head")

    def prepare(db, workspace, binding, cursor, history_cursor):
        calls.append("prepare")
        assert (workspace, binding, cursor, history_cursor) == (
            "workspace",
            "binding",
            None,
            "older",
        )
        if phase == "prepare_denied":
            raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "会话不可访问", 404)
        return prepared

    def read(actual):
        calls.append("read")
        assert actual is prepared
        return batch

    def authorize(db, actual):
        calls.append("reauthorize")
        assert actual is prepared
        if phase == "project_denied":
            raise DomainError("AGENT_RUNTIME_REPLACEMENT_FENCED", "身份已变化", 409)

    async def run(container, prepare_fn, read_fn, project_fn, *, history):
        assert history is True
        value = prepare_fn(None)
        return project_fn(None, value, read_fn(value))

    monkeypatch.setattr(conversations, "prepare_conversation_events", prepare)
    monkeypatch.setattr(conversations, "read_prepared_conversation_events", read)
    monkeypatch.setattr(conversations, "assert_prepared_conversation_events_current", authorize)
    monkeypatch.setattr(router, "run_formal_events", run)
    kwargs = dict(history_cursor="older", query=None, max_content_chars=2_000)
    if phase == "success":
        page = await router.agent_conversation_history("workspace", "binding", None, **kwargs)
        assert page["messages"][0]["event_id"] == "formal"
        assert calls == ["prepare", "read", "reauthorize"]
    else:
        with pytest.raises(DomainError):
            await router.agent_conversation_history("workspace", "binding", None, **kwargs)
        assert calls == (
            ["prepare"] if phase == "prepare_denied" else ["prepare", "read", "reauthorize"]
        )


@pytest.mark.parametrize("change", ["host", "root"])
def test_history_projection_fences_host_and_workspace_drift(monkeypatch, change):
    workspace = SimpleNamespace(id="workspace")
    binding = SimpleNamespace(host_kind="FLOW_NODE" if change == "host" else "AGENT_WORKSPACE")
    prepared = SimpleNamespace(
        locator=SimpleNamespace(
            key=SimpleNamespace(host_kind="AGENT_WORKSPACE"),
            handle=SimpleNamespace(workspace_root="/authorized"),
        )
    )
    monkeypatch.setattr(
        conversations,
        "_assert_prepared_conversation_hydration_current",
        lambda *_args: (workspace, binding),
    )
    monkeypatch.setattr(
        conversations, "_handle", lambda *_args: SimpleNamespace(workspace_root="/other")
    )
    with pytest.raises(DomainError) as caught:
        conversations.assert_prepared_conversation_events_current(None, prepared)
    assert caught.value.code == "AGENT_RUNTIME_REPLACEMENT_FENCED"
