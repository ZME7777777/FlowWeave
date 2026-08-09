from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Response, WebSocket, WebSocketDisconnect

from flowweave.bootstrap.container import Container
from flowweave.modules.conversations import public as service
from flowweave.modules.environments.infrastructure import docker
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, get_container, run_sync
from flowweave.shared.schemas import (
    ConversationCreateWrite,
    ConversationPatchWrite,
    MessageSendWrite,
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


@router.websocket("/agent-conversations/{conversation_id}/terminal")
async def conversation_terminal(
    websocket: WebSocket, conversation_id: str, container: ContainerDep
) -> None:
    """Attach a shell to the exact container backing the selected Agent conversation."""

    settings_token = bind_settings(container.settings)
    master = -1
    process = None
    try:
        async with container.database.session() as db:
            try:
                container_id = await db.run_sync(
                    lambda session: service.terminal_container_id(session, conversation_id)
                )
                await db.commit()
            except DomainError as exc:
                await db.rollback()
                await websocket.close(code=4409, reason=exc.message)
                return

        master, process = await asyncio.to_thread(
            docker.open_terminal,
            container_id,
            session_name=f"flowweave-{conversation_id}",
        )
        await websocket.accept()

        async def forward_output() -> None:
            while process.poll() is None:
                try:
                    chunk = await asyncio.to_thread(os.read, master, 8192)
                except OSError:
                    return
                if not chunk:
                    return
                await websocket.send_bytes(chunk)

        output = asyncio.create_task(forward_output())
        try:
            while process.poll() is None:
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
                    columns = max(2, min(int(value.get("columns", 80)), 400))
                    await asyncio.to_thread(docker.resize_terminal, master, rows, columns)
                elif value.get("type") == "input":
                    await asyncio.to_thread(os.write, master, str(value.get("data", "")).encode())
        except WebSocketDisconnect:
            pass
        finally:
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)
    finally:
        # This stops only the docker/tmux attachment. The named tmux session and
        # commands running inside it remain alive for the next browser connection.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 2)
            except TimeoutError:
                process.kill()
        if master >= 0:
            os.close(master)
        reset_settings(settings_token)


@router.patch("/agent-conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: str, payload: ConversationPatchWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.patch_conversation(session, conversation_id, payload),
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
