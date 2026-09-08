from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from flowweave.modules.runs.infrastructure import event_listener
from flowweave.shared.errors import DomainError


class _FakeListenConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self.notifications: asyncio.Queue[SimpleNamespace] = asyncio.Queue()

    async def execute(self, query: str) -> None:
        self.executed.append(query)

    async def close(self) -> None:
        self.closed = True

    async def notifies(self):  # type: ignore[no-untyped-def]
        while True:
            yield await self.notifications.get()


@pytest.mark.asyncio
async def test_run_event_listener_shares_one_listen_connection_and_bounds_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeListenConnection()
    connect_calls = 0

    async def connect(*_args: object, **_kwargs: object) -> _FakeListenConnection:
        nonlocal connect_calls
        connect_calls += 1
        return connection

    monkeypatch.setattr(event_listener.psycopg.AsyncConnection, "connect", connect)
    listener = event_listener.RunEventListener(
        "postgresql+psycopg://example",
        max_subscribers=2,
        subscriber_queue_size=1,
    )

    try:
        async with listener.subscribe("run-a") as first:
            async with listener.subscribe("run-b") as second:
                assert connect_calls == 1
                assert connection.executed == ["LISTEN flowweave_run_events"]
                assert listener.subscriber_count == 2

                with pytest.raises(DomainError, match="capacity") as error:
                    async with listener.subscribe("run-c"):
                        pass
                assert error.value.code == "RUN_EVENT_SSE_CAPACITY_EXHAUSTED"

                await listener._publish("run-a")
                assert await first.wait(0.1) is True
                assert await second.wait(0.01) is False

                await listener._publish("run-a")
                await listener._publish("run-a")
                assert first.dropped is True
                assert second.dropped is False
    finally:
        await listener.close()

    assert connection.closed is True
