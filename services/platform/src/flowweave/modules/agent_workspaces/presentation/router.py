from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Query,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field

from flowweave.bootstrap.container import Container
from flowweave.modules.agent_workspaces.application import conversations
from flowweave.modules.environments import public as environments
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, get_container, run_sync
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
ContainerDep = Annotated[Container, Depends(get_container)]


class _Write(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentWorkspaceSettingsWrite(_Write):
    default_model_provider_id: str | None = Field(default=None, max_length=36)


class AgentConversationCreateWrite(_Write):
    title: str | None = Field(default=None, max_length=240)


class AgentConversationPatchWrite(_Write):
    title: str = Field(min_length=1, max_length=200)


class AgentAttachmentReference(_Write):
    path: str = Field(min_length=1, max_length=300)
    image_data_url: str | None = Field(default=None, max_length=35_000_000)


def _empty_attachment_references() -> list[AgentAttachmentReference]:
    return []


class AgentMessageWrite(_Write):
    content: str = Field(min_length=1, max_length=200_000)
    # Model selection is intentionally deferred until this user turn.  The
    # browser can therefore let a user prepare the next turn without mutating
    # an idle Conversation merely by opening a select control.
    model_name: str | None = Field(default=None, min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    attachments: list[AgentAttachmentReference] = Field(
        default_factory=_empty_attachment_references, max_length=10
    )


class AgentConversationModelWrite(_Write):
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)


class AgentConversationForkWrite(_Write):
    event_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=240)


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


@router.get("/agent-workspaces/default")
async def get_default_agent_workspace(db: Db) -> dict[str, Any]:
    return await run_sync(db, conversations.default_workspace)


@router.get("/agent-workspaces/{workspace_id}")
async def get_agent_workspace(workspace_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: conversations.get_workspace(session, workspace_id))


@router.patch("/agent-workspaces/{workspace_id}/settings")
async def patch_agent_workspace_settings(
    workspace_id: str, payload: AgentWorkspaceSettingsWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.update_workspace_settings(
            session, workspace_id, payload.default_model_provider_id
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/runtime")
async def get_agent_workspace_runtime(workspace_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: conversations.runtime_status(session, workspace_id))


@router.get("/agent-workspaces/{workspace_id}/conversations")
async def list_agent_conversations(workspace_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: conversations.list_conversations(session, workspace_id)
    )


@router.post("/agent-workspaces/{workspace_id}/conversations", status_code=201)
async def create_agent_conversation(
    workspace_id: str,
    payload: AgentConversationCreateWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.create_conversation(
            session,
            workspace_id,
            payload.title,
            _key(idempotency_key, "create-agent-conversation", workspace_id),
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}")
async def get_agent_conversation(workspace_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.get_conversation(session, workspace_id, binding_id)
    )


@router.patch("/agent-workspaces/{workspace_id}/conversations/{binding_id}")
async def patch_agent_conversation(
    workspace_id: str, binding_id: str, payload: AgentConversationPatchWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.patch_conversation(
            session, workspace_id, binding_id, payload.title
        ),
    )


@router.delete(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}",
    status_code=204,
    response_class=Response,
)
async def delete_agent_conversation(
    workspace_id: str,
    binding_id: str,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    await run_sync(
        db,
        lambda session: conversations.delete_conversation(
            session,
            workspace_id,
            binding_id,
            _key(idempotency_key, "delete-agent-conversation", binding_id),
        ),
    )
    return Response(status_code=204)


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}/events")
async def agent_events(
    workspace_id: str,
    binding_id: str,
    db: Db,
    cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.events(session, workspace_id, binding_id, cursor)
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/messages", status_code=202
)
async def agent_message(
    workspace_id: str,
    binding_id: str,
    payload: AgentMessageWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    del idempotency_key
    return await run_sync(
        db,
        lambda session: conversations.message(
            session, workspace_id, binding_id, payload.content,
            model_name=payload.model_name,
            reasoning_effort=payload.reasoning_effort,
            attachments=tuple(item.model_dump(exclude_none=True) for item in payload.attachments),
        ),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/attachments", status_code=201
)
async def agent_attachment(
    workspace_id: str, binding_id: str, db: Db, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    return await run_sync(
        db,
        lambda session: conversations.upload_attachment(
            session,
            workspace_id,
            binding_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}/context")
async def agent_context(workspace_id: str, binding_id: str, db: Db) -> dict[str, int | str | None]:
    return await run_sync(
        db,
        lambda session: conversations.conversation_context(session, workspace_id, binding_id),
    )


@router.post("/agent-workspaces/{workspace_id}/conversations/{binding_id}/model")
async def agent_conversation_model(
    workspace_id: str, binding_id: str, payload: AgentConversationModelWrite, db: Db
) -> dict[str, str | None]:
    return await run_sync(
        db,
        lambda session: conversations.switch_conversation_model(
            session,
            workspace_id,
            binding_id,
            payload.model_name,
            payload.reasoning_effort,
        ),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/condense", status_code=202
)
async def agent_condense_conversation(
    workspace_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.condense_conversation(session, workspace_id, binding_id)
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/fork", status_code=201
)
async def agent_fork_conversation(
    workspace_id: str,
    binding_id: str,
    payload: AgentConversationForkWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.fork_conversation(
            session,
            workspace_id,
            binding_id,
            payload.event_id,
            payload.title,
            _key(idempotency_key, "fork-agent-conversation", binding_id),
        ),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/interrupt", status_code=202
)
async def agent_interrupt(workspace_id: str, binding_id: str, db: Db) -> dict[str, bool]:
    await run_sync(db, lambda session: conversations.interrupt(session, workspace_id, binding_id))
    return {"accepted": True}


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}/input-readiness")
async def agent_input_readiness(workspace_id: str, binding_id: str, db: Db) -> dict[str, bool]:
    return await run_sync(
        db,
        lambda session: conversations.input_readiness(session, workspace_id, binding_id),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/messages/{event_id}/rerun",
    status_code=202,
)
async def agent_rerun_edited_message(
    workspace_id: str,
    binding_id: str,
    event_id: str,
    payload: AgentMessageWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.rewrite_message(
            session, workspace_id, binding_id, event_id, payload.content
        ),
    )


@router.post("/agent-workspaces/{workspace_id}/conversations/{binding_id}/resume", status_code=202)
async def agent_resume(workspace_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.resume(session, workspace_id, binding_id)
    )


@router.websocket("/agent-workspaces/{workspace_id}/conversations/{binding_id}/stream")
async def agent_conversation_stream(
    websocket: WebSocket,
    workspace_id: str,
    binding_id: str,
    container: ContainerDep,
) -> None:
    settings_token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            try:
                adapter, handle = await db.run_sync(
                    lambda session: conversations.runtime_stream_details(
                        session, workspace_id, binding_id
                    )
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
            await websocket.close(code=1011, reason="Agent stream unavailable")
    finally:
        reset_settings(settings_token)


@router.websocket("/agent-workspaces/{workspace_id}/terminal")
async def agent_workspace_terminal(
    websocket: WebSocket, workspace_id: str, container: ContainerDep
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
                resource_name, runtime_id = await db.run_sync(
                    lambda session: conversations.terminal_resource_details(session, workspace_id)
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        terminal = await asyncio.to_thread(
            environments.open_managed_terminal,
            resource_name,
            resource_id=runtime_id,
            session_name=f"flowweave-agent-{workspace_id}",
            rows=rows,
            columns=columns,
        )
        await websocket.accept()

        async def forward_output() -> None:
            while True:
                chunk, eof = await asyncio.to_thread(terminal.read)
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
                        terminal.resize,
                        max(2, min(int(value.get("rows", 24)), 200)),
                        max(20, min(int(value.get("columns", 80)), 400)),
                    )
                elif value.get("type") == "input":
                    await asyncio.to_thread(terminal.write, str(value.get("data", "")).encode())
        except WebSocketDisconnect:
            pass
        finally:
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)
    finally:
        if terminal is not None:
            await asyncio.to_thread(terminal.close)
        reset_settings(settings_token)
