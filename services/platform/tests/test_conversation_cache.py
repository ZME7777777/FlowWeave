from __future__ import annotations

import asyncio

from flowweave.bootstrap.api import _conversation_mutation_binding_id
from flowweave.modules.agent_sessions.application.conversation_cache import (
    ConversationCacheKey,
    ConversationHydrationCache,
)


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


def test_terminal_hydration_is_reused() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        calls = 0

        async def load() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return hydration()

        assert await cache.get_or_load(key(), load) == hydration()
        assert await cache.get_or_load(key(), load) == hydration()
        assert calls == 1

    asyncio.run(scenario())


def test_current_invalidation_keeps_immutable_history() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        item = key()
        await cache.get_or_load(item, lambda: asyncio.sleep(0, result=hydration()))

        await cache.invalidate_binding(item.user_id, item.binding_id)
        assert await cache.get(item) is None
        assert len(cache._events) == 2
        assert len(cache._branches) == 1

        continued = hydration(leaf="event-3")
        await cache.get_or_load(item, lambda: asyncio.sleep(0, result=continued))
        assert await cache.get(item) == continued
        assert len(cache._events) == 3
        assert len(cache._branches) == 2

    asyncio.run(scenario())


def test_late_terminal_result_cannot_restore_invalidated_current() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        item = key()
        started = asyncio.Event()
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            started.set()
            await release.wait()
            return hydration()

        request = asyncio.create_task(cache.get_or_load(item, load))
        await started.wait()
        await cache.invalidate_current(item.scope)
        release.set()
        assert await request == hydration()
        assert await cache.get(item) is None
        assert len(cache._events) == 2

    asyncio.run(scenario())


def test_concurrent_loads_are_coalesced() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        calls = 0
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            nonlocal calls
            calls += 1
            await release.wait()
            return hydration()

        first = asyncio.create_task(cache.get_or_load(key(), load))
        second = asyncio.create_task(cache.get_or_load(key(), load))
        await asyncio.sleep(0)
        release.set()
        assert await first == hydration()
        assert await second == hydration()
        assert calls == 1

    asyncio.run(scenario())



def test_cancelled_waiter_does_not_cancel_shared_load() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            started.set()
            await release.wait()
            return hydration()

        cancelled = asyncio.create_task(cache.get_or_load(key(), load))
        survivor = asyncio.create_task(cache.get_or_load(key(), load))
        await started.wait()
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        release.set()
        assert await survivor == hydration()
        assert await cache.get(key()) == hydration()

    asyncio.run(scenario())


def test_running_hydration_is_not_reused_as_current() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        calls = 0

        async def load() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return hydration(running=True)

        await cache.get_or_load(key(), load)
        await cache.get_or_load(key(), load)
        assert calls == 2
        assert len(cache._events) == 2

    asyncio.run(scenario())


def test_paused_hydration_is_reused_but_confirmation_wait_is_not() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        paused = hydration()
        paused["readiness"] = {"ready": True, "execution_status": "paused"}
        waiting = hydration()
        waiting["readiness"] = {
            "ready": True,
            "execution_status": "waiting_for_confirmation",
        }

        await cache.get_or_load(key("paused"), lambda: asyncio.sleep(0, result=paused))
        await cache.get_or_load(key("waiting"), lambda: asyncio.sleep(0, result=waiting))

        assert await cache.get(key("paused")) == paused
        assert await cache.get(key("waiting")) is None

    asyncio.run(scenario())


def test_scope_lookup_avoids_identity_resolution_on_terminal_hit() -> None:
    async def scenario() -> None:
        cache = ConversationHydrationCache()
        item = key()
        await cache.get_or_load(item, lambda: asyncio.sleep(0, result=hydration()))

        assert await cache.get_current_for_scope(item.scope) == hydration()
        assert await cache.get_current_for_scope(key("other").scope) is None

    asyncio.run(scenario())


def test_ttl_capacity_and_user_identity_are_isolated() -> None:
    async def scenario() -> None:
        now = 0.0
        cache = ConversationHydrationCache(
            terminal_ttl_seconds=5,
            history_ttl_seconds=10,
            max_current=1,
            max_branches=1,
            max_events=2,
            clock=lambda: now,
        )
        first = key("first", user_id="alice")
        second = key("second", user_id="bob")
        await cache.get_or_load(first, lambda: asyncio.sleep(0, result=hydration()))
        await cache.get_or_load(second, lambda: asyncio.sleep(0, result=hydration(leaf="event-3")))
        assert await cache.get(first) is None
        assert await cache.get(second) is not None
        now = 6
        assert await cache.get(second) is None
        assert len(cache._branches) == 1
        now = 11
        assert await cache.get(second) is None
        assert len(cache._branches) == 0
        assert len(cache._events) == 0

    asyncio.run(scenario())


def test_only_execution_mutations_invalidate_current_snapshot() -> None:
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
