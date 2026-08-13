from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Response, WebSocket, WebSocketDisconnect

from flowweave.bootstrap.container import Container
from flowweave.modules.conversations import public as service
from flowweave.modules.environments import public as environments
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, get_container, run_sync
from flowweave.shared.schemas import (
    ConversationCondenseWrite,
    ConversationCreateWrite,
    ConversationForkWrite,
    ConversationPatchWrite,
    ConversationReviseWrite,
    ConversationStopWrite,
    MessageSendWrite,
    RuntimeSubagentTaskRead,
)
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
Actor = Annotated[str | None, Header(alias="X-Actor-ID")]
ContainerDep = Annotated[Container, Depends(get_container)]


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


@router.get("/node-attempts/{attempt_id}/conversations")
async def conversations(attempt_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.list_conversations(session, attempt_id))


@router.post("/node-attempts/{attempt_id}/conversations", status_code=202)
async def create_conversation(
    attempt_id: str,
    payload: ConversationCreateWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.create_conversation(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "create-conversation", attempt_id),
        ),
    )


@router.get("/agent-conversations/{conversation_id}")
async def conversation(conversation_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.get_conversation(session, conversation_id))


@router.websocket("/agent-conversations/{conversation_id}/stream")
async def conversation_stream(
    websocket: WebSocket, conversation_id: str, container: ContainerDep
) -> None:
    """Proxy safe, transient Runtime text deltas without exposing Runtime credentials."""

    settings_token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            try:
                adapter, handle = await db.run_sync(
                    lambda session: service.runtime_stream_details(session, conversation_id)
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
            # Runtime connectivity is best-effort; durable messages remain the fallback.
            await websocket.close(code=1011, reason="Runtime stream unavailable")
    finally:
        reset_settings(settings_token)


@router.get(
    "/agent-conversations/{conversation_id}/subagents",
    response_model=list[RuntimeSubagentTaskRead],
)
async def subagents(conversation_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.list_subagents(session, conversation_id))


@router.websocket("/agent-conversations/{conversation_id}/terminal")
async def conversation_terminal(
    websocket: WebSocket, conversation_id: str, container: ContainerDep
) -> None:
    """Attach a shell to the exact container backing the selected Agent conversation."""

    settings_token = bind_settings(container.settings)
    terminal: environments.ManagedTerminal | None = None
    try:
        initial_rows = max(2, min(int(websocket.query_params.get("rows", "24")), 200))
        initial_columns = max(20, min(int(websocket.query_params.get("columns", "80")), 400))
    except ValueError:
        initial_rows, initial_columns = 24, 80
    try:
        async with container.database.session() as db:
            try:
                resource_name, sandbox_id, environment_id = await db.run_sync(
                    lambda session: service.terminal_resource_details(session, conversation_id)
                )
                await db.commit()
            except DomainError as exc:
                await db.rollback()
                await websocket.close(code=4409, reason=exc.message)
                return

        try:
            terminal = await asyncio.to_thread(
                environments.open_managed_terminal,
                resource_name,
                resource_id=sandbox_id,
                environment_id=environment_id,
                session_name=f"flowweave-{conversation_id}",
                rows=initial_rows,
                columns=initial_columns,
            )
        except DomainError as exc:
            await websocket.close(code=4409, reason=exc.message)
            return
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
                    rows = max(2, min(int(value.get("rows", 24)), 200))
                    columns = max(20, min(int(value.get("columns", 80)), 400))
                    await asyncio.to_thread(active_terminal.resize, rows, columns)
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
            # This closes only the attachment. The controller-owned tmux session
            # remains alive for a later browser reconnection.
            await asyncio.to_thread(terminal.close)
        reset_settings(settings_token)


@router.patch("/agent-conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: str, payload: ConversationPatchWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.patch_conversation(session, conversation_id, payload),
    )


@router.post("/agent-conversations/{conversation_id}/stop", status_code=202)
async def stop_conversation(
    conversation_id: str,
    payload: ConversationStopWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.stop_conversation(
            session,
            conversation_id,
            payload,
            _key(idempotency_key, "stop-conversation", conversation_id),
        ),
    )


@router.post("/agent-conversations/{conversation_id}/condense", status_code=202)
async def condense_conversation(
    conversation_id: str,
    payload: ConversationCondenseWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.request_conversation_condensation(
            session,
            conversation_id,
            payload,
            _key(idempotency_key, "condense-conversation", conversation_id),
            actor,
        ),
    )


@router.delete("/agent-conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation(conversation_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_conversation(session, conversation_id))
    return Response(status_code=204)


@router.get("/agent-conversations/{conversation_id}/messages")
async def messages(
    conversation_id: str,
    db: Db,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    return await run_sync(
        db,
        lambda session: service.list_messages(session, conversation_id, after_sequence, limit),
    )


@router.get("/agent-messages/{message_id}/workspace-image", response_class=Response)
async def workspace_image(message_id: str, source: str, db: Db) -> Response:
    reference = await run_sync(
        db, lambda session: service.workspace_image_reference(session, message_id, source)
    )
    content = await asyncio.to_thread(reference.path.read_bytes)
    return Response(
        content=content,
        media_type=reference.media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(reference.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/agent-messages/{message_id}/attachments/{attachment_id}", response_class=Response)
async def message_attachment(
    message_id: str, attachment_id: str, db: Db, download: bool = False
) -> Response:
    reference = await run_sync(
        db,
        lambda session: service.message_attachment_reference(session, message_id, attachment_id),
    )
    content = await asyncio.to_thread(reference.path.read_bytes)
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=reference.media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(reference.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/agent-conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: str,
    payload: MessageSendWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    workspace = await run_sync(
        db, lambda session: service.attachment_workspace(session, conversation_id)
    )
    prepared = await asyncio.to_thread(service.prepare_message_content, payload, workspace)
    try:
        return await run_sync(
            db,
            lambda session: service.send_message(
                session,
                conversation_id,
                payload,
                _key(idempotency_key, "send-message", conversation_id),
                actor,
                prepared_parts=prepared.parts,
            ),
        )
    except BaseException:
        await asyncio.to_thread(service.discard_prepared_message_content, prepared)
        raise


@router.post("/agent-messages/{message_id}/fork", status_code=202)
async def fork_conversation(
    message_id: str,
    payload: ConversationForkWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.fork_conversation(
            session,
            message_id,
            payload,
            _key(idempotency_key, "fork-conversation", message_id),
        ),
    )


@router.post("/agent-messages/{message_id}/revise", status_code=202)
async def revise_message(
    message_id: str,
    payload: ConversationReviseWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.revise_message(
            session,
            message_id,
            payload,
            _key(idempotency_key, "revise-message", message_id),
            actor,
        ),
    )


@router.post("/agent-messages/{message_id}/retry", status_code=202)
async def retry_message(
    message_id: str,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.retry_message(
            session,
            message_id,
            _key(idempotency_key, "retry-message", message_id),
        ),
    )


@router.post("/agent-messages/{message_id}/steer", status_code=202)
async def steer_message(
    message_id: str,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.steer_message(
            session,
            message_id,
            _key(idempotency_key, "steer-message", message_id),
        ),
    )


@router.post("/agent-messages/{message_id}/cancel-queued", status_code=202)
async def cancel_queued_message(
    message_id: str,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.cancel_queued_message(
            session,
            message_id,
            _key(idempotency_key, "cancel-queued-message", message_id),
        ),
    )
