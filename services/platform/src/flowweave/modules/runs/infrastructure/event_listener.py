from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg

from flowweave.shared.errors import DomainError

_CHANNEL = "flowweave_run_events"
logger = logging.getLogger(__name__)


@dataclass(eq=False, slots=True)
class RunEventSubscription:
    """One bounded local fan-out recipient; it never owns a database connection."""

    run_id: str
    queue: asyncio.Queue[None]
    dropped: bool = False
    closed: bool = False

    async def wait(self, timeout_seconds: float) -> bool:
        """Return on a matching notification or the heartbeat deadline."""

        if self.closed or self.dropped:
            return False
        try:
            await asyncio.wait_for(self.queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return not self.closed and not self.dropped

    def close(self, *, dropped: bool = False) -> None:
        self.closed = True
        self.dropped = self.dropped or dropped


class RunEventListener:
    """One PostgreSQL LISTEN connection per process with bounded SSE fan-out."""

    def __init__(
        self,
        database_url: str,
        *,
        max_subscribers: int = 256,
        subscriber_queue_size: int = 8,
    ) -> None:
        self.connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._max_subscribers = max_subscribers
        self._subscriber_queue_size = subscriber_queue_size
        self._connection: psycopg.AsyncConnection[tuple[object, ...]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscriptions: dict[str, set[RunEventSubscription]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return sum(len(items) for items in self._subscriptions.values())

    async def start(self) -> None:
        """Open exactly one autocommit LISTEN connection for this process."""

        async with self._lock:
            if self._closed:
                raise RuntimeError("Run event listener is closed")
            if self._task is not None and not self._task.done():
                return
            connection = await psycopg.AsyncConnection.connect(
                self.connection_url,
                autocommit=True,
            )
            try:
                await connection.execute(f"LISTEN {_CHANNEL}")
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
            self._task = asyncio.create_task(self._consume(connection))

    async def _consume(self, connection: psycopg.AsyncConnection[tuple[object, ...]]) -> None:
        try:
            async for notification in connection.notifies():
                await self._publish(notification.payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Run event LISTEN connection stopped", exc_info=True)
        finally:
            async with self._lock:
                if self._connection is connection:
                    self._connection = None
                for subscriptions in self._subscriptions.values():
                    for subscription in subscriptions:
                        subscription.close(dropped=True)
                self._subscriptions.clear()
            await connection.close()

    async def _publish(self, run_id: str) -> None:
        async with self._lock:
            subscriptions = tuple(self._subscriptions.get(run_id, ()))
            for subscription in subscriptions:
                if subscription.closed:
                    self._subscriptions[run_id].discard(subscription)
                    continue
                try:
                    subscription.queue.put_nowait(None)
                except asyncio.QueueFull:
                    # A slow browser reconnects from Last-Event-ID; its cursor
                    # read remains the authoritative recovery path.
                    subscription.close(dropped=True)
                    self._subscriptions[run_id].discard(subscription)
            if not self._subscriptions.get(run_id):
                self._subscriptions.pop(run_id, None)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[RunEventSubscription]:
        await self.start()
        async with self._lock:
            if self.subscriber_count >= self._max_subscribers:
                raise DomainError(
                    "RUN_EVENT_SSE_CAPACITY_EXHAUSTED",
                    "The run event stream capacity is exhausted",
                    503,
                    {"max_subscribers": self._max_subscribers},
                )
            subscription = RunEventSubscription(
                run_id=run_id,
                queue=asyncio.Queue(maxsize=self._subscriber_queue_size),
            )
            self._subscriptions.setdefault(run_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            subscription.close()
            async with self._lock:
                subscriptions = self._subscriptions.get(run_id)
                if subscriptions is not None:
                    subscriptions.discard(subscription)
                    if not subscriptions:
                        self._subscriptions.pop(run_id, None)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            task = self._task
            self._task = None
            connection = self._connection
            self._connection = None
            for subscriptions in self._subscriptions.values():
                for subscription in subscriptions:
                    subscription.close()
            self._subscriptions.clear()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if connection is not None:
            await connection.close()
