from __future__ import annotations

import asyncio

import pytest

from flowweave.bootstrap.api import _conversation_mutation_binding_id
from flowweave.modules.agent_sessions.application.conversation_cache import (
    ConversationCacheKey,
    ConversationHydrationCache,
)


@pytest.fixture(autouse=True)
def database() -> None:
    """Cache contracts are process-local and do not require the database fixture."""


def key(binding_id: str = "binding", *, user_id: str = "user") -> ConversationCacheKey:
    return ConversationCacheKey(
        user_id=user_id,
        host_kind="AGENT_WORKSPACE",
        host_id="workspace",
        binding_id=binding_id,
        runtime_session_id="runtime",
        runtime_generation="generation-1",
        conversation_id=f"conversation-{binding_id}",
    )


def hydration(*, leaf: str = "event-2", running: bool = False) -> dict[str, object]:
    return {
        "events": {
            "events": [
                {"id": "event-1", "event_type": "MESSAGE", "payload": {"content": "question"}},
                {"id": leaf, "event_type": "MESSAGE", "payload": {"content": "answer"}},
            ],
            "next_cursor": leaf,
            "history_cursor": None,
            "result": {"status": "RUNNING" if running else "COMPLETED"},
        },
        "context": {"used_tokens": 42},
        "readiness": {"ready": not running, "execution_status": "running" if running else "idle"},
    }


def test_terminal_hydration_is_refreshed_for_each_request() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        calls = 0

        async def load() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return hydration(leaf=f"event-{calls + 1}")

        assert await cache.refresh(key(), load) == hydration(leaf="event-2")
        assert await cache.refresh(key(), load) == hydration(leaf="event-3")
        assert calls == 2

    asyncio.run(scenario())


def test_concurrent_refreshes_are_coalesced() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        calls = 0
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            nonlocal calls
            calls += 1
            await release.wait()
            return hydration()

        first = asyncio.create_task(cache.refresh(key(), load))
        second = asyncio.create_task(cache.refresh(key(), load))
        await asyncio.sleep(0)
        release.set()
        assert await first == hydration()
        assert await second == hydration()
        assert calls == 1

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_refresh() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            started.set()
            await release.wait()
            return hydration()

        cancelled = asyncio.create_task(cache.refresh(key(), load))
        survivor = asyncio.create_task(cache.refresh(key(), load))
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        assert await survivor == hydration()

    asyncio.run(scenario())


def test_execution_mutation_paths_keep_current_state_uncached() -> None:
    direct = "/api/v1/agent-workspaces/workspace/conversations/binding"
    node = "/api/v1/flow-runs/run/node-attempts/attempt/agent-sessions/binding"

    for suffix in (
        "/messages",
        "/messages/event/rerun",
        "/pending-confirmation/decision",
        "/model",
        "/condense",
        "/interrupt",
        "/resume",
    ):
        assert _conversation_mutation_binding_id("POST", direct + suffix) == "binding"
        assert _conversation_mutation_binding_id("POST", node + suffix) == "binding"

    assert _conversation_mutation_binding_id("DELETE", direct) == "binding"
    assert _conversation_mutation_binding_id("DELETE", node) == "binding"
    assert _conversation_mutation_binding_id("GET", direct + "/hydration") is None
    assert _conversation_mutation_binding_id("PATCH", direct) is None
    assert _conversation_mutation_binding_id("POST", direct + "/streaming-migration") is None

    assert _conversation_mutation_binding_id("POST", direct + "/attachments") is None
