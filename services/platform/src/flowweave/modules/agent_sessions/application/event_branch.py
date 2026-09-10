"""Formal OpenHands event-tree helpers used by conversation commands."""

from __future__ import annotations

from flowweave.runtime.base import RuntimeEvent


def latest_user_message_on_active_branch(
    events: tuple[RuntimeEvent, ...], leaf_event_id: str | None
) -> RuntimeEvent | None:
    """Return the closest user message on the formal active parent chain.

    OpenHands event windows can be returned newest-first at the transport
    boundary. Event ordering must never decide which sent message is editable.
    """

    if not leaf_event_id:
        return None
    by_id = {event.cursor: event for event in events}
    if len(by_id) != len(events):
        return None
    current_id = leaf_event_id
    seen: set[str] = set()
    while current_id != "__root__":
        if current_id in seen:
            return None
        seen.add(current_id)
        event = by_id.get(current_id)
        if event is None:
            return None
        if event.event_type == "MESSAGE" and str(event.payload.get("source") or "").lower() in {
            "user",
            "human",
        }:
            return event
        parent_id = event.payload.get("parent_id")
        if not isinstance(parent_id, str):
            return None
        current_id = parent_id
    return None
