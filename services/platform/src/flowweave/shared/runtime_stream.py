"""Bounded, disconnect-aware forwarding of an already authorized native stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from anyio import move_on_after
from fastapi import WebSocket


async def forward_runtime_stream(
    websocket: WebSocket,
    stream: AsyncIterator[dict[str, Any]],
    project: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
    revalidate: Callable[[], Awaitable[None]] | None = None,
) -> None:
    event_task: asyncio.Task[dict[str, Any]] | None = None
    receive_task: asyncio.Task[Any] | None = None
    validation_task: asyncio.Task[None] | None = None

    async def next_event() -> dict[str, Any]:
        return await anext(stream)

    async def validate_later() -> None:
        await asyncio.sleep(15)
        if revalidate is not None:
            await revalidate()

    try:
        event_task = asyncio.create_task(next_event())
        receive_task = asyncio.create_task(websocket.receive())
        if revalidate is not None:
            validation_task = asyncio.create_task(validate_later())
        while True:
            tasks: list[asyncio.Task[Any]] = [
                task for task in (event_task, receive_task, validation_task) if task is not None
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                if receive_task.result().get("type") == "websocket.disconnect":
                    return
                receive_task = asyncio.create_task(websocket.receive())
            if validation_task is not None and validation_task in done:
                validation_task.result()
                validation_task = asyncio.create_task(validate_later())
            if event_task in done:
                try:
                    event = event_task.result()
                except StopAsyncIteration:
                    return
                projected = await project(event)
                if projected is not None:
                    await websocket.send_json(projected)
                event_task = asyncio.create_task(next_event())
    finally:
        tasks = [task for task in (event_task, receive_task, validation_task) if task is not None]
        for task in tasks:
            task.cancel()
        # ASGI may cancel the handler together with the browser disconnect.
        # Allow the already-cancelled readers and upstream relay to close.
        with move_on_after(5, shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
