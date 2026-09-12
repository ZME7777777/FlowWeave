"""Formal OpenHands event-tree helpers used by conversation commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeHandle

_MAX_ACTIVE_BRANCH_EVENTS = 10_000


def complete_active_branch(
    read_active_events: Callable[[RuntimeHandle], RuntimeEventBatch],
    handle: RuntimeHandle,
) -> RuntimeEventBatch:
    """Read one complete formal HEAD branch through OpenHands history cursors.

    This is intentionally a server-side hydration primitive.  Browsers must
    never have to stitch native pages together before they can build the user
    message index needed for display, Fork, or rewrite.  ``history_cursor``
    remains an OpenHands identity and is never persisted by FlowWeave.
    """

    pages: list[RuntimeEventBatch] = []
    events_by_id: dict[str, RuntimeEvent] = {}
    leaf_event_id: str | None = None
    history_cursor: str | None = None
    visited_history_cursors: set[str] = set()

    while True:
        batch = read_active_events(replace(handle, history_cursor=history_cursor))
        if leaf_event_id is None:
            leaf_event_id = batch.cursor
        elif batch.cursor != leaf_event_id:
            raise ValueError("OpenHands active branch changed during hydration")
        for event in batch.events:
            existing = events_by_id.get(event.cursor)
            if existing is not None and existing != event:
                raise ValueError("OpenHands returned conflicting event identities")
            events_by_id[event.cursor] = event
        pages.append(batch)
        history_cursor = batch.history_cursor
        if not history_cursor:
            # Oldest pages are read last.  Preserve the same chronological
            # page ordering previously produced by browser-side merging.
            events = tuple(event for page in reversed(pages) for event in page.events)
            newest = pages[0]
            return RuntimeEventBatch(
                events=events,
                cursor=leaf_event_id,
                result=newest.result,
                cursor_anchor_found=newest.cursor_anchor_found,
                task_usage=newest.task_usage,
                usage=newest.usage,
                history_cursor=None,
            )
        if history_cursor in visited_history_cursors:
            break
        visited_history_cursors.add(history_cursor)

    raise ValueError("OpenHands active branch hydration encountered a cursor cycle")


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


def latest_user_message_across_active_branch_pages(
    read_active_events: Callable[[RuntimeHandle], RuntimeEventBatch],
    handle: RuntimeHandle,
) -> RuntimeEvent | None:
    """Find the latest user message even when the active branch spans pages.

    OpenHands returns a bounded active-branch window. ``history_cursor`` is
    the formal missing parent at the older edge, so following it preserves the
    native parent chain without inferring order from timestamps or transport
    pages. Rewrites use this path because a long agent turn can put its user
    message more than one event page behind the formal leaf.
    """

    events_by_id: dict[str, RuntimeEvent] = {}
    leaf_event_id: str | None = None
    history_cursor: str | None = None
    visited_history_cursors: set[str] = set()

    while len(events_by_id) < _MAX_ACTIVE_BRANCH_EVENTS:
        batch = read_active_events(replace(handle, history_cursor=history_cursor))
        if leaf_event_id is None:
            leaf_event_id = batch.cursor
        elif batch.cursor != leaf_event_id:
            return None
        for event in batch.events:
            existing = events_by_id.get(event.cursor)
            if existing is not None and existing != event:
                return None
            events_by_id[event.cursor] = event

        target = latest_user_message_on_active_branch(tuple(events_by_id.values()), leaf_event_id)
        if target is not None:
            return target

        history_cursor = batch.history_cursor
        if not history_cursor or history_cursor in visited_history_cursors:
            return None
        visited_history_cursors.add(history_cursor)

    return None
