from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import conversations
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database() -> None:
    """Reference assembly uses controlled native events without database I/O."""


@pytest.fixture
def resolve(monkeypatch):
    workspace = SimpleNamespace(id="workspace")
    source = SimpleNamespace(openhands_conversation_id="native-source")
    handle = RuntimeHandle(job_id="source", conversation_id="native-source")

    def binding(_db, workspace_id, binding_id):
        assert workspace_id == workspace.id
        assert binding_id == "source-binding"
        return source

    monkeypatch.setattr(conversations, "_binding", binding)
    monkeypatch.setattr(conversations, "_handle", lambda *_args: handle)

    def run(events, reference=None):
        class Runtime(MockRuntime):
            def read_active_events(self, actual):
                assert actual.conversation_id == handle.conversation_id
                return RuntimeEventBatch(events=tuple(events), cursor="native-head")

            def read_event(self, actual, event_id):
                assert actual.conversation_id == handle.conversation_id
                return next((event for event in events if event.cursor == event_id), None)

        monkeypatch.setattr(conversations, "get_runtime", lambda: Runtime())
        return conversations._resolve_message_references(
            None, workspace, None, (reference or {"source_binding_id": "source-binding"},)
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
    expected = "\n\n".join(contents)[-10_000:].strip()
    assert resolved == ({"event_id": "native-head", "content": expected},)
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
    assert resolve(events)[0]["content"] == "正式用户正文\n\n最新回复"


def test_empty_conversation_reference_still_fails(resolve):
    with pytest.raises(DomainError) as caught:
        resolve([])
    assert caught.value.code == "AGENT_CONVERSATION_REFERENCE_UNAVAILABLE"


def test_selected_reference_preserves_exact_event_and_range(resolve):
    event = RuntimeEvent(
        cursor="selected-event", event_type="MESSAGE", payload={"content": "前文 目标内容 后文"}
    )
    assert resolve(
        [event],
        {
            "source_binding_id": "source-binding",
            "source_event_id": "selected-event",
            "selection": {"compact_start": 2, "compact_end": 6},
        },
    ) == ({"event_id": "selected-event", "content": "目标内容"},)


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
