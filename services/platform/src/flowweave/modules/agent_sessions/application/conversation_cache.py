from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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



class ConversationHydrationCache:
    """Coalesce concurrent Runtime hydration reads within one API worker.

    Every hydration reads the Runtime's latest event window and readiness. The
    browser owns cached historical pages; this process-local coordinator never
    returns a previously completed hydration as current state.
    """

    def __init__(self) -> None:
        self._inflight: dict[ConversationCacheKey, asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def refresh(
        self,
        key: ConversationCacheKey,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Read the latest Runtime projection, coalescing only concurrent reads.

        A terminal snapshot is never returned as current state because another
        API worker may have observed a newer Runtime event after it was cached.
        """

        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._load(key, loader))
                self._inflight[key] = task
        return copy.deepcopy(await asyncio.shield(task))

    async def invalidate_binding(self, user_id: str, binding_id: str) -> None:
        """Compatibility hook; completed hydration state is never retained."""

    async def invalidate_identity(self, key: ConversationCacheKey) -> None:
        """Compatibility hook; completed hydration state is never retained."""

    async def close(self) -> None:
        async with self._lock:
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _load(
        self,
        key: ConversationCacheKey,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            return await loader()
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)


__all__ = ("ConversationCacheKey", "ConversationHydrationCache")
