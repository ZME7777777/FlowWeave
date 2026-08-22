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
    ConversationAskAgentWrite,
    ConversationCreateWrite,
    ConversationGoalWrite,
    ConversationPatchWrite,
    ConversationQuestionWrite,
)
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
Actor = Annotated[str | None, Header(alias="X-Actor-ID")]
ContainerDep = Annotated[Container, Depends(get_container)]


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


@router.get("/node-attempts/{attempt_id}/conversations")
async def list_flow_run_conversations(attempt_id: str, db: Db) -> list[dict[str, Any]]:
    """Compatibility route: the Attempt resolves a FlowRun and owns nothing."""

    return await run_sync(db, lambda session: conversations.list_conversations(session, attempt_id))


@router.post("/node-attempts/{attempt_id}/conversations", status_code=201)
async def create_flow_run_conversation(
    attempt_id: str,
    payload: ConversationCreateWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.create_conversation(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "create-flow-run-conversation", attempt_id),
        ),
    )


@router.get("/agent-conversations/{binding_id}")
async def get_flow_run_conversation(binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: conversations.get_conversation(session, binding_id))


@router.patch("/agent-conversations/{binding_id}")
async def label_flow_run_conversation(
    binding_id: str, payload: ConversationPatchWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.patch_conversation(session, binding_id, payload.title),
    )


@router.get("/agent-conversations/{binding_id}/messages")
async def live_conversation_events(
    binding_id: str,
    db: Db,
    cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    """Compatibility path returning live OpenHands events, never platform messages."""

    return await run_sync(
        db,
        lambda session: conversations.read_conversation_events(
            session, binding_id, cursor=cursor
        ),
    )


@router.post("/agent-conversations/{binding_id}/messages", status_code=202)
async def ask_conversation(
    binding_id: str,
    payload: ConversationQuestionWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.send_question(
            session,
            binding_id,
            payload,
            _key(idempotency_key, "ask-flow-run-conversation", binding_id),
            actor,
        ),
    )


@router.post("/agent-conversations/{binding_id}/stop", status_code=202)
async def stop_conversation(binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.stop_conversation(session, binding_id)
    )


@router.post("/agent-conversations/{binding_id}/condense", status_code=202)
async def condense_conversation(binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.condense_conversation(session, binding_id)
    )


@router.post("/agent-conversations/{binding_id}/goal", status_code=202)
async def control_conversation_goal(
    binding_id: str, payload: ConversationGoalWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.control_goal(
            session,
            binding_id,
            action=payload.action,
            objective=payload.objective,
            max_iterations=payload.max_iterations,
        ),
    )


@router.post("/agent-conversations/{binding_id}/ask-agent")
async def ask_agent_diagnostic(
    binding_id: str, payload: ConversationAskAgentWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.ask_agent(
            session,
            binding_id,
            question=payload.question,
            timeout_seconds=payload.timeout_seconds,
        ),
    )


@router.websocket("/agent-conversations/{binding_id}/stream")
async def conversation_stream(
    websocket: WebSocket, binding_id: str, container: ContainerDep
) -> None:
    settings_token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            try:
                adapter, handle = await db.run_sync(
                    lambda session: conversations.runtime_stream_details(session, binding_id)
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        with runtime_context(container.runtime):
            runtime = runtime_for(adapter, handle)
        await websocket.accept()
        try:
            async for event in runtime.stream_events(handle):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:
            await websocket.close(code=1011, reason="Runtime stream unavailable")
    finally:
        reset_settings(settings_token)


@router.websocket("/agent-conversations/{binding_id}/terminal")
async def conversation_terminal(
    websocket: WebSocket, binding_id: str, container: ContainerDep
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
                resource_name, runtime_id, environment_id = await db.run_sync(
                    lambda session: conversations.terminal_resource_details(session, binding_id)
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        terminal = await asyncio.to_thread(
            environments.open_managed_terminal,
            resource_name,
            resource_id=runtime_id,
            environment_id=environment_id,
            session_name=f"flowweave-{binding_id}",
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
