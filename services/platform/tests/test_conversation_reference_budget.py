from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import conversations
from flowweave.runtime.base import RuntimeEvent, RuntimeHandle
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database() -> None:
    """Reference assembly uses controlled native events without database I/O."""


@pytest.fixture
def resolve(monkeypatch):
    workspace = SimpleNamespace(id="workspace")
    source = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000002",
        openhands_conversation_id="00000000-0000-0000-0000-000000000001",
        lifecycle="ACTIVE",
        display_title="来源会话",
    )
    handle = RuntimeHandle(job_id="source", conversation_id="00000000-0000-0000-0000-000000000001")

    def binding(_db, workspace_id, binding_id):
        assert workspace_id == workspace.id
        assert binding_id == "00000000-0000-0000-0000-000000000002"
        return source

    monkeypatch.setattr(conversations, "_binding", binding)
    monkeypatch.setattr(conversations, "_handle", lambda *_args: handle)

    def run(events, reference=None):
        class Runtime(MockRuntime):
            def read_active_events(self, actual):
                pytest.fail("Whole references must not expand history")

            def read_event(self, actual, event_id):
                assert actual.conversation_id == handle.conversation_id
                return next((event for event in events if event.cursor == event_id), None)

        monkeypatch.setattr(conversations, "get_runtime", lambda: Runtime())
        return conversations._resolve_message_references(
            None,
            workspace,
            None,
            (reference or {"source_binding_id": "00000000-0000-0000-0000-000000000002"},),
        )

    return run


@pytest.mark.parametrize(
    "contents",
    [
        ("短提问", "短回复"),
        ("旧" * 6_000, "新" * 6_000),
        ("旧" * 4_999, "新" * 4_999),
        ("旧" * 4_999, "新" * 5_000),
        ("旧" * 9_999, "新"),
        ("旧", "新" * 10_000),
        ("长" * 12_000,),
        tuple(f"第{index}条消息" for index in range(1_500)),
    ],
)
def test_whole_conversation_reference_fits_budget_and_round_trips(resolve, contents):
    events = [
        RuntimeEvent(cursor=f"event-{index}", event_type="MESSAGE", payload={"content": text})
        for index, text in enumerate(contents)
    ]
    resolved = resolve(events)
    assert resolved == (
        {
            "kind": "conversation",
            "event_id": "",
            "content": "来源会话",
            "title": "来源会话",
            "source_binding_id": "00000000-0000-0000-0000-000000000002",
            "source_conversation_id": "00000000-0000-0000-0000-000000000001",
            "history_tool": "read_referenced_conversation",
        },
    )
    assert len(resolved[0]["content"]) <= 10_000

    prompt, image_urls = conversations.message_payload("继续下一个切片", (), resolved)
    visible, references, files, annotations = conversations.project_conversation_references(prompt)
    assert visible == "继续下一个切片"
    assert references == resolved
    assert (image_urls, files, annotations) == ((), (), ())


def test_whole_conversation_reference_keeps_only_visible_context(resolve):
    prompt, _ = conversations.message_payload(
        "正式用户正文", (), ({"event_id": "old-reference", "content": "历史引用背景"},)
    )
    events = [
        RuntimeEvent(cursor="user", event_type="MESSAGE", payload={"content": prompt}),
        RuntimeEvent(cursor="tool", event_type="TOOL_RESULT", payload={"content": "工具输出"}),
        RuntimeEvent(cursor="reply", event_type="MESSAGE", payload={"content": "最新回复"}),
    ]
    assert resolve(events)[0]["content"] == "来源会话"


def test_empty_conversation_reference_remains_metadata_only(resolve):
    assert resolve([])[0]["kind"] == "conversation"


def test_selected_reference_preserves_exact_event_and_range(resolve):
    event = RuntimeEvent(
        cursor="selected-event", event_type="MESSAGE", payload={"content": "前文 目标内容 后文"}
    )
    selection = {"compact_start": 2, "compact_end": 6}
    resolved = resolve(
        [event],
        {
            "source_binding_id": "00000000-0000-0000-0000-000000000002",
            "source_event_id": "selected-event",
            "selection": selection,
        },
    )

    assert resolved == (
        {
            "event_id": "selected-event",
            "content": "目标内容",
            "source_binding_id": "00000000-0000-0000-0000-000000000002",
            "selection": selection,
        },
    )
    prompt, _ = conversations.message_payload("继续", (), resolved)
    assert conversations.project_conversation_references(prompt)[1] == resolved


def test_explicit_oversized_reference_and_total_budget_still_fail():
    for references, code in (
        (({"event_id": "one", "content": "字" * 10_001},), "AGENT_CONVERSATION_REFERENCE_INVALID"),
        (
            tuple({"event_id": str(index), "content": "字" * 10_000} for index in range(3)),
            "AGENT_CONVERSATION_REFERENCE_TOO_LARGE",
        ),
    ):
        with pytest.raises(DomainError) as caught:
            conversations.message_payload("继续", (), references)
        assert caught.value.code == code
