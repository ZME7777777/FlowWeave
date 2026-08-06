from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Header, Query

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


@router.post("/agent-conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: str,
    payload: MessageSendWrite,
    db: Db,
    actor: Actor = None,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.send_message(
            session,
            conversation_id,
            payload,
            _key(idempotency_key, "send-message", conversation_id),
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
