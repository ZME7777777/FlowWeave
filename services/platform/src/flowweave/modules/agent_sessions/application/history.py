"""Bounded, read-only message pages for authenticated history consumers."""

from typing import Any

from flowweave.modules.agent_sessions.application import conversations
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch
from flowweave.shared.errors import DomainError


def visible_message(event: RuntimeEvent) -> tuple[str, str] | None:
    source = event.payload.get("source")
    if (
        event.event_type != "MESSAGE"
        or not isinstance(source, str)
        or source not in {"user", "human", "agent", "assistant"}
    ):
        return None
    raw = event.payload.get("content")
    if not isinstance(raw, str):
        return None
    content = conversations.project_conversation_references(raw)[0].strip()
    return (source, content) if content else None


def message_content(
    event: RuntimeEvent, *, content_offset: int = 0, max_content_chars: int = 2_000
) -> dict[str, Any]:
    """Slice the visible text of one authorized, immutable native event by code point."""

    visible = visible_message(event)
    if visible is None:
        raise DomainError("AGENT_HISTORY_EVENT_UNAVAILABLE", "该事件没有可读取的消息正文", 404)
    if not 1 <= max_content_chars <= 12_000:
        raise DomainError("AGENT_HISTORY_LIMIT_INVALID", "正文长度必须在 1 到 12000 之间", 422)
    source, content = visible
    if not 0 <= content_offset <= len(content):
        raise DomainError("AGENT_HISTORY_RANGE_INVALID", "正文读取位置无效", 422)
    end = min(len(content), content_offset + max_content_chars)
    return {
        "event_id": event.cursor,
        "source": source,
        "content": content[content_offset:end],
        "content_chars": len(content),
        "content_offset": content_offset,
        "next_content_offset": end if end < len(content) else None,
        "truncated": end < len(content),
    }


def message_page(
    batch: RuntimeEventBatch, *, query: str | None = None, max_content_chars: int = 2_000
) -> dict[str, Any]:
    """Filter one native window; never walk history or invent pagination cursors."""

    if not 1 <= max_content_chars <= 12_000:
        raise DomainError("AGENT_HISTORY_LIMIT_INVALID", "正文长度必须在 1 到 12000 之间", 422)
    needle = (query or "").strip().casefold()
    messages: list[dict[str, str | int | bool]] = []
    for event in batch.events:
        visible = visible_message(event)
        if visible is None:
            continue
        source, content = visible
        if not content or needle and needle not in content.casefold():
            continue
        messages.append(
            {
                "event_id": event.cursor,
                "source": source,
                "content": content[:max_content_chars],
                "content_chars": len(content),
                "truncated": len(content) > max_content_chars,
            }
        )
    return {
        "messages": messages,
        "next_cursor": batch.cursor,
        "history_cursor": batch.history_cursor,
        "query_scope": "current_page",
        "max_content_chars": max_content_chars,
    }
