from __future__ import annotations

import asyncio
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Response

from flowweave.modules.conversations import public as service
from flowweave.shared.http import Db, IdempotencyKey, command_key, run_sync
from flowweave.shared.schemas import (
    ConversationCreateWrite,
    ConversationPatchWrite,
    MessageSendWrite,
)

router = APIRouter()
Actor = Annotated[str | None, Header(alias="X-Actor-ID")]


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


@router.get(
    "/agent-messages/{message_id}/attachments/{attachment_id}", response_class=Response
)
async def message_attachment(
    message_id: str, attachment_id: str, db: Db, download: bool = False
) -> Response:
    reference = await run_sync(
        db,
        lambda session: service.message_attachment_reference(
            session, message_id, attachment_id
        ),
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
