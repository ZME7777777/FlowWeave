from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg

_CHANNEL = "flowweave_run_events"


@dataclass(slots=True)
class RunEventSubscription:
    """One PostgreSQL LISTEN connection dedicated to an SSE client."""

    connection: psycopg.AsyncConnection[tuple[object, ...]]

    async def wait(self, run_id: str, timeout_seconds: float) -> bool:
        """Wait for this run's notification; false means the heartbeat deadline elapsed."""

        async for notification in self.connection.notifies(timeout=timeout_seconds):
            if notification.payload == run_id:
                return True
        return False


class RunEventListener:
    """Creates short-lived LISTEN subscriptions without using the SQLAlchemy pool."""

    def __init__(self, database_url: str) -> None:
        self.connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[RunEventSubscription]:
        connection = await psycopg.AsyncConnection.connect(
            self.connection_url,
            autocommit=True,
        )
        try:
            await connection.execute(f"LISTEN {_CHANNEL}")
            yield RunEventSubscription(connection)
        finally:
            await connection.close()
