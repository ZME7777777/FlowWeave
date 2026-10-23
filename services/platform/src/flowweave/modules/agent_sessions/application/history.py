"""Bounded, read-only message pages for authenticated history consumers."""

from typing import Any

from flowweave.modules.agent_sessions.application import conversations
from flowweave.runtime.base import RuntimeEventBatch
from flowweave.shared.errors import DomainError


def message_page(
    batch: RuntimeEventBatch, *, query: str | None = None, max_content_chars: int = 2_000
) -> dict[str, Any]:
    """Filter one native window; never walk history or invent pagination cursors."""

    if not 1 <= max_content_chars <= 10_000:
        raise DomainError("AGENT_HISTORY_LIMIT_INVALID", "正文长度必须在 1 到 10000 之间", 422)
    needle = (query or "").strip().casefold()
    messages: list[dict[str, str | int | bool]] = []
    for event in batch.events:
        source = event.payload.get("source")
        if event.event_type != "MESSAGE" or source not in {"user", "human", "agent", "assistant"}:
            continue
        raw = event.payload.get("content")
        if not isinstance(raw, str):
            continue
        content = conversations.project_conversation_references(raw)[0].strip()
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
