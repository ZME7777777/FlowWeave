from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any


@dataclass(frozen=True, slots=True)
class ConversationCacheKey:
    user_id: str
    host_kind: str
    host_id: str
    binding_id: str
    runtime_session_id: str
    runtime_generation: str
    conversation_id: str

    @property
    def scope(self) -> ConversationCacheScope:
        return ConversationCacheScope(
            user_id=self.user_id,
            host_kind=self.host_kind,
            host_id=self.host_id,
            binding_id=self.binding_id,
        )


@dataclass(frozen=True, slots=True)
class ConversationCacheScope:
    user_id: str
    host_kind: str
    host_id: str
    binding_id: str


@dataclass(frozen=True, slots=True)
class _EventKey:
    identity: ConversationCacheKey
    event_id: str


@dataclass(frozen=True, slots=True)
class _BranchKey:
    identity: ConversationCacheKey
    leaf_event_id: str


@dataclass(slots=True)
class _ExpiringValue:
    value: Any
    expires_at: float


@dataclass(slots=True)
class _CurrentState:
    branch_key: _BranchKey
    context: dict[str, Any]
    readiness: dict[str, Any]
    expires_at: float
    epoch: int


class ConversationHydrationCache:
    """Bounded process-local cache for immutable history and terminal snapshots.

    Branches and events survive current-state invalidation. Only a terminal
    hydration becomes reusable as the current response. In-flight loads are
    coalesced and an epoch fence prevents a response started before a command
    from restoring stale current state after that command advances the session.
    """

    def __init__(
        self,
        *,
        terminal_ttl_seconds: float = 300,
        history_ttl_seconds: float = 3600,
        max_current: int = 256,
        max_branches: int = 1024,
        max_events: int = 20_000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(terminal_ttl_seconds, history_ttl_seconds) <= 0:
            raise ValueError("cache TTLs must be positive")
        if min(max_current, max_branches, max_events) <= 0:
            raise ValueError("cache capacities must be positive")
        self._terminal_ttl = terminal_ttl_seconds
        self._history_ttl = history_ttl_seconds
        self._max_current = max_current
        self._max_branches = max_branches
        self._max_events = max_events
        self._clock = clock
        self._current: OrderedDict[ConversationCacheKey, _CurrentState] = OrderedDict()
        self._branches: OrderedDict[_BranchKey, _ExpiringValue] = OrderedDict()
        self._events: OrderedDict[_EventKey, _ExpiringValue] = OrderedDict()
        self._epochs: dict[ConversationCacheScope, int] = {}
        self._inflight: dict[ConversationCacheKey, asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: ConversationCacheKey) -> dict[str, Any] | None:
        async with self._lock:
            return self._get_locked(key, self._clock())

    async def get_current_for_scope(self, scope: ConversationCacheScope) -> dict[str, Any] | None:
        async with self._lock:
            now = self._clock()
            self._prune_locked(now)
            keys = [key for key in self._current if key.scope == scope]
            if len(keys) != 1:
                return None
            return self._get_locked(keys[0], now)

    async def get_or_load(
        self,
        key: ConversationCacheKey,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._lock:
            cached = self._get_locked(key, self._clock())
            if cached is not None:
                return cached
            task = self._inflight.get(key)
            if task is None:
                epoch = self._epochs.get(key.scope, 0)
                task = asyncio.create_task(self._load(key, epoch, loader))
                self._inflight[key] = task
        return copy.deepcopy(await asyncio.shield(task))

    async def invalidate_current(self, scope: ConversationCacheScope) -> None:
        async with self._lock:
            self._invalidate_current_locked(scope)

    async def invalidate_binding(self, user_id: str, binding_id: str) -> None:
        async with self._lock:
            scopes = {
                key.scope
                for key in (*self._current.keys(), *self._inflight.keys())
                if key.user_id == user_id and key.binding_id == binding_id
            }
            for scope in scopes:
                self._invalidate_current_locked(scope)

    async def invalidate_identity(self, key: ConversationCacheKey) -> None:
        """Remove current and historical data after identity deletion/replacement."""

        async with self._lock:
            self._epochs[key.scope] = self._epochs.get(key.scope, 0) + 1
            self._current.pop(key, None)
            for branch_key in [item for item in self._branches if item.identity == key]:
                self._branches.pop(branch_key, None)
            for event_key in [item for item in self._events if item.identity == key]:
                self._events.pop(event_key, None)

    async def close(self) -> None:
        async with self._lock:
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._current.clear()
            self._branches.clear()
            self._events.clear()
            self._epochs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _load(
        self,
        key: ConversationCacheKey,
        epoch: int,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            hydration = await loader()
            async with self._lock:
                self._store_locked(key, hydration, epoch, self._clock())
            return hydration
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    def _invalidate_current_locked(self, scope: ConversationCacheScope) -> None:
        self._epochs[scope] = self._epochs.get(scope, 0) + 1
        for key in [item for item in self._current if item.scope == scope]:
            self._current.pop(key, None)

    def _get_locked(self, key: ConversationCacheKey, now: float) -> dict[str, Any] | None:
        self._prune_locked(now)
        current = self._current.get(key)
        if current is None or current.epoch != self._epochs.get(key.scope, 0):
            return None
        branch = self._branches.get(current.branch_key)
        if branch is None:
            self._current.pop(key, None)
            return None
        event_ids = branch.value["event_ids"]
        events: list[dict[str, Any]] = []
        for event_id in event_ids:
            item = self._events.get(_EventKey(key, event_id))
            if item is None:
                self._current.pop(key, None)
                return None
            self._events.move_to_end(_EventKey(key, event_id))
            events.append(copy.deepcopy(item.value))
        self._current.move_to_end(key)
        self._branches.move_to_end(current.branch_key)
        projected = copy.deepcopy(branch.value["metadata"])
        projected["events"] = events
        return {
            "events": projected,
            "context": copy.deepcopy(current.context),
            "readiness": copy.deepcopy(current.readiness),
        }

    def _store_locked(
        self,
        key: ConversationCacheKey,
        hydration: dict[str, Any],
        epoch: int,
        now: float,
    ) -> None:
        projected = hydration.get("events")
        readiness = hydration.get("readiness")
        context = hydration.get("context")
        if (
            not isinstance(projected, dict)
            or not isinstance(readiness, dict)
            or not isinstance(context, dict)
        ):
            return
        events = projected.get("events")
        leaf = projected.get("next_cursor")
        if not isinstance(events, list) or not isinstance(leaf, str) or not leaf:
            return
        event_ids: list[str] = []
        expires_at = now + self._history_ttl
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                return
            event_id = event["id"]
            event_ids.append(event_id)
            event_key = _EventKey(key, event_id)
            self._events[event_key] = _ExpiringValue(copy.deepcopy(event), expires_at)
            self._events.move_to_end(event_key)
        metadata = {
            name: copy.deepcopy(value) for name, value in projected.items() if name != "events"
        }
        branch_key = _BranchKey(key, leaf)
        self._branches[branch_key] = _ExpiringValue(
            {"event_ids": tuple(event_ids), "metadata": metadata}, expires_at
        )
        self._branches.move_to_end(branch_key)
        self._trim_locked(self._events, self._max_events)
        self._trim_locked(self._branches, self._max_branches)

        if not self._terminal(readiness, projected):
            return
        if self._epochs.get(key.scope, 0) != epoch:
            return
        self._current[key] = _CurrentState(
            branch_key=branch_key,
            context=copy.deepcopy(context),
            readiness=copy.deepcopy(readiness),
            expires_at=now + self._terminal_ttl,
            epoch=epoch,
        )
        self._current.move_to_end(key)
        self._trim_locked(self._current, self._max_current)

    @staticmethod
    def _terminal(readiness: dict[str, Any], projected: dict[str, Any]) -> bool:
        status = str(readiness.get("execution_status") or "").strip().lower()
        result = projected.get("result")
        result_status = (
            str(result.get("status") or "").strip().upper() if isinstance(result, dict) else ""
        )
        return (
            readiness.get("ready") is True
            and status
            not in {
                "unknown",
                "starting",
                "running",
                "executing",
                "stopping",
                "waiting_for_confirmation",
                "pausing",
                "resuming",
            }
            and result_status not in {"RUNNING", "STARTING", "EXECUTING"}
        )

    def _prune_locked(self, now: float) -> None:
        for key in [item for item, value in self._current.items() if value.expires_at <= now]:
            self._current.pop(key, None)
        for store in (self._branches, self._events):
            for key in [item for item, value in store.items() if value.expires_at <= now]:
                store.pop(key, None)

    @staticmethod
    def _trim_locked(store: OrderedDict[Any, Any], capacity: int) -> None:
        while len(store) > capacity:
            store.popitem(last=False)


__all__ = (
    "ConversationCacheKey",
    "ConversationCacheScope",
    "ConversationHydrationCache",
)
