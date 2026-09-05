from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, WebSocket, WebSocketDisconnect

from flowweave.bootstrap.container import Container
from flowweave.modules.conversations import public as conversations
from flowweave.modules.environments import public as environments
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, get_container, run_sync
from flowweave.shared.schemas import (
    ConversationPatchWrite,
    ConversationQuestionWrite,
    FlowRunConversationCreateWrite,
)
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
Actor = Annotated[str | None, Header(alias="X-Actor-ID")]
ContainerDep = Annotated[Container, Depends(get_container)]


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


async def _forward_runtime_events(websocket: WebSocket, runtime: Any, handle: Any) -> None:
    """Forward one Runtime stream and close it promptly when the client leaves."""

    stream = runtime.stream_events(handle)
    event_task: asyncio.Task[Any] | None = None
    receive_task: asyncio.Task[Any] | None = None

    async def next_event() -> dict[str, Any]:
        return await anext(stream)

    try:
        event_task = asyncio.create_task(next_event())
        receive_task = asyncio.create_task(websocket.receive())
        while True:
            done, _ = await asyncio.wait(
                (event_task, receive_task), return_when=asyncio.FIRST_COMPLETED
            )
            if receive_task in done:
                message = receive_task.result()
                if message.get("type") == "websocket.disconnect":
                    return
                receive_task = asyncio.create_task(websocket.receive())
            if event_task in done:
                try:
                    event = event_task.result()
                except StopAsyncIteration:
                    return
                await websocket.send_json(event)
                event_task = asyncio.create_task(next_event())
    finally:
        for task in (event_task, receive_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (event_task, receive_task) if task is not None),
            return_exceptions=True,
        )
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


@router.get("/flow-runs/{flow_run_id}/conversations")
async def list_flow_run_conversations(flow_run_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: conversations.list_flow_run_conversations(session, flow_run_id)
    )


@router.post("/flow-runs/{flow_run_id}/conversations", status_code=201)
async def create_flow_run_conversation(
    flow_run_id: str,
    payload: FlowRunConversationCreateWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.create_flow_run_conversation(
            session,
            flow_run_id,
            payload,
            _key(idempotency_key, "create-flow-run-conversation", flow_run_id),
        ),
    )


@router.get("/flow-runs/{flow_run_id}/conversations/{binding_id}")
async def get_flow_run_conversation(flow_run_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.get_flow_run_conversation(session, flow_run_id, binding_id),
    )


@router.patch("/flow-runs/{flow_run_id}/conversations/{binding_id}")
async def label_flow_run_conversation(
    flow_run_id: str,
    binding_id: str,
    payload: ConversationPatchWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.patch_flow_run_conversation(
            session, flow_run_id, binding_id, payload.title
        ),
    )


@router.get("/flow-runs/{flow_run_id}/conversations/{binding_id}/events")
async def live_conversation_events(
    flow_run_id: str,
    binding_id: str,
    db: Db,
    cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    """Return live OpenHands events without persisting a platform cursor."""

    return await run_sync(
        db,
        lambda session: conversations.read_flow_run_conversation_events(
            session, flow_run_id, binding_id, cursor=cursor
        ),
    )


@router.post(
    "/flow-runs/{flow_run_id}/conversations/{binding_id}/questions",
    status_code=202,
)
async def ask_conversation(
    flow_run_id: str,
    binding_id: str,
    payload: ConversationQuestionWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.send_flow_run_question(
            session,
            flow_run_id,
            binding_id,
            payload,
            _key(idempotency_key, "ask-flow-run-conversation", binding_id),
            actor,
        ),
    )


@router.post(
    "/flow-runs/{flow_run_id}/conversations/{binding_id}/stop",
    status_code=202,
)
async def stop_conversation(flow_run_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.stop_flow_run_conversation(session, flow_run_id, binding_id),
    )


@router.websocket("/flow-runs/{flow_run_id}/conversations/{binding_id}/stream")
async def conversation_stream(
    websocket: WebSocket,
    flow_run_id: str,
    binding_id: str,
    container: ContainerDep,
) -> None:
    settings_token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            try:
                adapter, handle = await db.run_sync(
                    lambda session: conversations.flow_run_runtime_stream_details(
                        session, flow_run_id, binding_id
                    )
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        with runtime_context(container.runtime):
            runtime = runtime_for(adapter, handle)
        await websocket.accept()
        try:
            await _forward_runtime_events(websocket, runtime, handle)
        except WebSocketDisconnect:
            pass
        except Exception:
            await websocket.close(code=1011, reason="Runtime stream unavailable")
    finally:
        reset_settings(settings_token)


@router.websocket("/flow-runs/{flow_run_id}/conversations/{binding_id}/terminal")
async def conversation_terminal(
    websocket: WebSocket,
    flow_run_id: str,
    binding_id: str,
    container: ContainerDep,
) -> None:
    settings_token = bind_settings(container.settings)
    terminal: environments.ManagedTerminal | None = None
    try:
        try:
            rows = max(2, min(int(websocket.query_params.get("rows", "24")), 200))
            columns = max(20, min(int(websocket.query_params.get("columns", "80")), 400))
        except ValueError:
            rows, columns = 24, 80
        async with container.database.session() as db:
            try:
                resource_name, runtime_id, working_directory = await db.run_sync(
                    lambda session: conversations.flow_run_terminal_details(
                        session, flow_run_id, binding_id
                    )
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        terminal = await asyncio.to_thread(
            environments.open_managed_terminal,
            resource_name,
            resource_id=runtime_id,
            session_name=f"flowweave-{binding_id}",
            working_dir=working_directory,
            rows=rows,
            columns=columns,
        )
        await websocket.accept()
        active_terminal = terminal

        async def forward_output() -> None:
            while True:
                chunk, eof = await asyncio.to_thread(active_terminal.read)
                if chunk:
                    await websocket.send_bytes(chunk)
                if eof:
                    return

        output = asyncio.create_task(forward_output())
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = {"type": "input", "data": text}
                if value.get("type") == "resize":
                    await asyncio.to_thread(
                        active_terminal.resize,
                        max(2, min(int(value.get("rows", 24)), 200)),
                        max(20, min(int(value.get("columns", 80)), 400)),
                    )
                elif value.get("type") == "input":
                    await asyncio.to_thread(
                        active_terminal.write, str(value.get("data", "")).encode()
                    )
        except WebSocketDisconnect:
            pass
        finally:
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)
    finally:
        if terminal is not None:
            await asyncio.to_thread(terminal.close)
        reset_settings(settings_token)
