"""FlowRun-node gateway for shared Agent-session operations.

These routes are scoped by immutable FlowRun and node-attempt lineage.  They
are deliberately narrow while host-neutral workspace/file APIs are extracted:
no Agent Workspace ownership or browser-visible Runtime identity leaks here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, cast
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
from flowweave.modules.agent_sessions import public as agent_sessions
from flowweave.modules.agent_sessions.application.runtime_config import resolve_session_config
from flowweave.modules.environments import public as environments
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, IdempotencyKey, command_key, get_container, run_sync
from flowweave.shared.schemas import ConversationPatchWrite
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
ContainerDep = Annotated[Container, Depends(get_container)]


class _Write(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeSessionCreateWrite(_Write):
    title: str | None = Field(default=None, max_length=160)
    work_directory_id: str | None = Field(default=None, min_length=1, max_length=36)


class NodeAttachmentReference(_Write):
    path: str = Field(min_length=1, max_length=300)
    image_data_url: str | None = Field(default=None, max_length=35_000_000)
    filename: str | None = Field(default=None, max_length=240)
    mime_type: str | None = Field(default=None, max_length=200)
    byte_size: int | None = Field(default=None, ge=0, le=25 * 1024 * 1024)


class NodeSessionBootstrapFullWrite(_Write):
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    client_question_id: str | None = Field(default=None, min_length=1, max_length=100)
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    work_directory_id: str | None = Field(default=None, min_length=1, max_length=36)
    content: str | list[dict[str, Any]] = Field(max_length=200_000)
    attachments: list[NodeAttachmentReference] = cast(
        list[NodeAttachmentReference], Field(default_factory=list, max_length=10)
    )
    capability_version_ids: list[str] = Field(default_factory=list, max_length=30)


class NodeSessionMessageWrite(_Write):
    content: str = Field(max_length=200_000)
    attachments: list[NodeAttachmentReference] = cast(
        list[NodeAttachmentReference], Field(default_factory=list, max_length=10)
    )


class NodeConfirmationDecisionWrite(_Write):
    expected_pending_digest: str = Field(min_length=1, max_length=128)
    accept: bool
    reason: str = Field(min_length=1, max_length=2_000)


class NodeForkWrite(_Write):
    event_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=240)


class NodeCapabilitySelectionWrite(_Write):
    capability_version_ids: list[str] = Field(default_factory=list, max_length=30)


class NodeCapabilityAddWrite(_Write):
    capability_version_id: str = Field(min_length=1, max_length=36)


class NodeSessionModelWrite(_Write):
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)


class FlowRunWorkDirectoryCreateWrite(_Write):
    display_name: str = Field(min_length=1, max_length=160)
    selected_paths: list[str] = Field(min_length=1, max_length=20)


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


async def _forward_runtime_events(websocket: WebSocket, runtime: Any, handle: Any) -> None:
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


_BASE = "/flow-runs/{flow_run_id}/node-attempts/{attempt_id}/agent-sessions"


@router.get(f"{_BASE}/host")
async def node_session_host(flow_run_id: str, attempt_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.node_host_details(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id
        ),
    )


@router.get(f"{_BASE}/runtime")
async def node_session_runtime(flow_run_id: str, attempt_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.node_runtime_status(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id
        ),
    )


def _default_workspace_id(session: Any) -> str:
    workspace = agent_sessions.conversations.default_workspace(session)
    return str(workspace["id"])


@router.get(f"{_BASE}/capabilities")
async def node_session_capabilities(
    flow_run_id: str, attempt_id: str, db: Db
) -> list[dict[str, str]]:
    return await run_sync(
        db,
        lambda session: (
            agent_sessions.resolve_flow_node_session_host(
                session,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                require_start_permission=False,
            ),
            agent_sessions.conversations.workspace_capabilities(
                session, _default_workspace_id(session)
            ),
        )[1],
    )


@router.put(f"{_BASE}/capabilities")
async def replace_node_session_capabilities(
    flow_run_id: str, attempt_id: str, payload: NodeCapabilitySelectionWrite, db: Db
) -> list[dict[str, str]]:
    return await run_sync(
        db,
        lambda session: (
            agent_sessions.resolve_flow_node_session_host(
                session,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                require_start_permission=False,
            ),
            agent_sessions.conversations.replace_workspace_capabilities(
                session, _default_workspace_id(session), tuple(payload.capability_version_ids)
            ),
        )[1],
    )


@router.post(f"{_BASE}/capabilities/{{capability_version_id}}/mcp-readiness")
async def node_session_mcp_readiness(
    flow_run_id: str, attempt_id: str, capability_version_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: (
            agent_sessions.resolve_flow_node_session_host(
                session,
                flow_run_id=flow_run_id,
                attempt_id=attempt_id,
                require_start_permission=False,
            ),
            agent_sessions.conversations.probe_workspace_mcp_readiness(
                session, _default_workspace_id(session), capability_version_id
            ),
        )[1],
    )


@router.post(f"{_BASE}/{{binding_id}}/capabilities", status_code=201)
async def add_node_session_capability(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeCapabilityAddWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.add_node_conversation_capability(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            capability_version_id=payload.capability_version_id,
        ),
    )


@router.get(_BASE)
async def list_node_sessions(flow_run_id: str, attempt_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.list_node_session_views(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id
        ),
    )


@router.post(_BASE, status_code=201)
async def create_node_session(
    flow_run_id: str,
    attempt_id: str,
    payload: NodeSessionCreateWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    def create(session: Any) -> dict[str, Any]:
        created = agent_sessions.flow_node_conversations.create_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            title=payload.title,
            work_directory_id=payload.work_directory_id,
            idempotency_key=_key(idempotency_key, "create-node-agent-session", attempt_id),
        )
        return agent_sessions.flow_node_conversations.get_node_session_view(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=str(created["id"]),
        )

    return await run_sync(db, create)


@router.post(f"{_BASE}/bootstrap", status_code=201)
async def bootstrap_node_session(
    flow_run_id: str,
    attempt_id: str,
    payload: NodeSessionBootstrapFullWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    legacy_image_urls: list[str] = []
    if isinstance(payload.content, str):
        content = payload.content
    else:
        content = "\n".join(
            str(item.get("text") or "") for item in payload.content if item.get("type") == "text"
        )
        for item in payload.content:
            if item.get("type") != "attachment":
                continue
            mime_type = str(item.get("mime_type") or "application/octet-stream")
            encoded = str(item.get("content_base64") or "")
            if not mime_type.startswith("image/") or not encoded:
                raise DomainError(
                    "ATTACHMENT_TYPE_UNSUPPORTED",
                    "请先通过附件上传接口添加非图片文件",
                    422,
                )
            legacy_image_urls.append(f"data:{mime_type};base64,{encoded}")
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.bootstrap_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            content=content,
            attachments=tuple(
                cast(dict[str, str | int], item.model_dump(exclude_none=True))
                for item in payload.attachments
            ),
            legacy_image_urls=tuple(legacy_image_urls),
            conversation_id=payload.conversation_id,
            work_directory_id=payload.work_directory_id,
            session_config=resolve_session_config(
                session,
                model_provider_id=payload.model_provider_id,
                model_name=payload.model_name,
                reasoning_effort=payload.reasoning_effort,
                capability_version_ids=tuple(payload.capability_version_ids),
            ),
            idempotency_key=_key(idempotency_key, "bootstrap-node-agent-session", attempt_id),
        ),
    )


@router.get(f"{_BASE}/workspace")
async def node_session_workspace(
    flow_run_id: str,
    attempt_id: str,
    db: Db,
    binding_id: str | None = Query(default=None),
    work_directory_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_workspace.details(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            work_directory_id=work_directory_id,
        ),
    )


@router.get(f"{_BASE}/workspace/file")
async def node_session_workspace_file(
    flow_run_id: str,
    attempt_id: str,
    db: Db,
    path: str = Query(...),
    binding_id: str | None = Query(default=None),
    work_directory_id: str | None = Query(default=None),
    download: bool = Query(default=False),
) -> Response:
    content, content_type, filename = await run_sync(
        db,
        lambda session: agent_sessions.flow_node_workspace.read_file(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            work_directory_id=work_directory_id,
            path=path,
        ),
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get(f"{_BASE}/work-directories")
async def list_flow_run_work_directories(
    flow_run_id: str, attempt_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: {
            "root": {
                "kind": "ROOT",
                "display_name": "节点工作目录",
                "working_directory": agent_sessions.resolve_flow_node_session_host(
                    session,
                    flow_run_id=flow_run_id,
                    attempt_id=attempt_id,
                    require_start_permission=False,
                ).session.working_directory,
            },
            "items": [],
        },
    )


@router.post(f"{_BASE}/work-directories", status_code=201)
async def create_flow_run_work_directory(
    flow_run_id: str,
    attempt_id: str,
    payload: FlowRunWorkDirectoryCreateWrite,
    db: Db,
) -> dict[str, Any]:
    def create(session: Any) -> dict[str, Any]:
        agent_sessions.resolve_flow_node_session_host(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            require_start_permission=False,
        )
        raise DomainError("NODE_WORK_DIRECTORY_FIXED", "节点会话固定使用当前 Attempt 工作目录", 409)

    return await run_sync(db, create)


@router.get(f"{_BASE}/{{binding_id}}")
async def get_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.get_node_session_view(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        ),
    )


@router.patch(f"{_BASE}/{{binding_id}}")
async def patch_node_session(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: ConversationPatchWrite,
    db: Db,
) -> dict[str, Any]:
    def patch(session: Any) -> dict[str, Any]:
        agent_sessions.flow_node_conversations.patch_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            title=payload.title,
        )
        return agent_sessions.flow_node_conversations.get_node_session_view(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        )

    return await run_sync(db, patch)


@router.delete(f"{_BASE}/{{binding_id}}", status_code=204)
async def delete_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> Response:
    await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.delete_node_conversation(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        ),
    )
    return Response(status_code=204)


@router.get(f"{_BASE}/{{binding_id}}/events")
async def node_session_events(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    db: Db,
    cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.read_node_conversation_events(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            cursor=cursor,
        ),
    )


@router.get(f"{_BASE}/{{binding_id}}/input-readiness")
async def node_session_input_readiness(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, bool]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.node_input_readiness(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        ),
    )


@router.get(f"{_BASE}/{{binding_id}}/context")
async def node_session_context(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.node_conversation_context(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        ),
    )


@router.get(f"{_BASE}/{{binding_id}}/pending-confirmation")
async def node_pending_confirmation(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.node_pending_confirmation(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/pending-confirmation/decision", status_code=202)
async def decide_node_confirmation(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeConfirmationDecisionWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.decide_node_confirmation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            expected_pending_digest=payload.expected_pending_digest,
            accept=payload.accept,
            reason=payload.reason,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/model")
async def switch_node_session_model(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeSessionModelWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.switch_node_conversation_model(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            model_provider_id=payload.model_provider_id,
            model_name=payload.model_name,
            reasoning_effort=payload.reasoning_effort,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/messages", status_code=202)
async def node_session_message(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeSessionMessageWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    del idempotency_key
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.send_node_message(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            content=payload.content,
            attachments=tuple(
                cast(dict[str, str | int], item.model_dump(exclude_none=True))
                for item in payload.attachments
            ),
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/messages/{{event_id}}/rerun", status_code=202)
async def rerun_node_message(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    event_id: str,
    payload: NodeSessionMessageWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.rerun_node_message(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            event_id=event_id,
            content=payload.content,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/fork", status_code=201)
async def fork_node_session(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeForkWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.fork_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            event_id=payload.event_id,
            title=payload.title,
            idempotency_key=_key(idempotency_key, "fork-node-agent-session", binding_id),
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/streaming-migration", status_code=201)
async def migrate_node_session(
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    payload: NodeSessionModelWrite,
    db: Db,
) -> dict[str, Any]:
    # New node conversations already have the shared streaming callback.  The
    # route is retained for parity and returns the same scoped conversation.
    del payload
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.get_node_session_view(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/attachments", status_code=201)
async def upload_node_attachment(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.upload_node_attachment(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=file.file.read(25 * 1024 * 1024 + 1),
        ),
    )


@router.post(f"{_BASE}/attachments", status_code=201)
async def upload_node_draft_attachment(
    flow_run_id: str,
    attempt_id: str,
    db: Db,
    file: Annotated[UploadFile, File()],
    conversation_id: str | None = Query(default=None, min_length=36, max_length=36),
    work_directory_id: str | None = Query(default=None),
) -> dict[str, Any]:
    del work_directory_id
    content = await file.read(25 * 1024 * 1024 + 1)
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.upload_node_attachment(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            attachment_owner_id=conversation_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/condense", status_code=202)
async def condense_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.condense_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/interrupt", status_code=202)
async def interrupt_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, bool]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.interrupt_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/resume", status_code=202)
async def resume_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.resume_node_conversation(
            session,
            flow_run_id=flow_run_id,
            attempt_id=attempt_id,
            binding_id=binding_id,
        ),
    )


@router.post(f"{_BASE}/{{binding_id}}/stop", status_code=202)
async def stop_node_session(
    flow_run_id: str, attempt_id: str, binding_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: agent_sessions.flow_node_conversations.stop_node_conversation(
            session, flow_run_id=flow_run_id, attempt_id=attempt_id, binding_id=binding_id
        ),
    )


@router.websocket(f"{_BASE}/{{binding_id}}/stream")
async def node_session_stream(
    websocket: WebSocket,
    flow_run_id: str,
    attempt_id: str,
    binding_id: str,
    container: ContainerDep,
) -> None:
    token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            try:
                adapter, handle = await db.run_sync(
                    lambda session: (
                        agent_sessions.flow_node_conversations.node_runtime_stream_details(
                            session,
                            flow_run_id=flow_run_id,
                            attempt_id=attempt_id,
                            binding_id=binding_id,
                        )
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
        reset_settings(token)


@router.websocket(f"{_BASE}/terminal")
@router.websocket(f"{_BASE}/{{binding_id}}/terminal")
async def node_session_terminal(
    websocket: WebSocket,
    flow_run_id: str,
    attempt_id: str,
    container: ContainerDep,
    binding_id: str | None = None,
) -> None:
    token = bind_settings(container.settings)
    terminal: environments.ManagedTerminal | None = None
    try:
        try:
            rows = max(2, min(int(websocket.query_params.get("rows", "24")), 200))
            columns = max(20, min(int(websocket.query_params.get("columns", "80")), 400))
        except ValueError:
            rows, columns = 24, 80
        async with container.database.session() as db:
            try:

                def terminal_details(session: Any) -> tuple[str, str, str, str]:
                    if binding_id:
                        return (
                            *agent_sessions.flow_node_conversations.node_terminal_resource_details(
                                session,
                                flow_run_id=flow_run_id,
                                attempt_id=attempt_id,
                                binding_id=binding_id,
                            ),
                            agent_sessions.flow_node_workspace.conversation_working_directory(
                                session,
                                flow_run_id=flow_run_id,
                                attempt_id=attempt_id,
                                binding_id=binding_id,
                            ),
                        )
                    return (
                        agent_sessions.flow_node_conversations.node_draft_terminal_resource_details(
                            session,
                            flow_run_id=flow_run_id,
                            attempt_id=attempt_id,
                        )
                    )

                resource_name, runtime_id, environment_id, working_directory = await db.run_sync(
                    terminal_details
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        terminal = await asyncio.to_thread(
            environments.open_managed_terminal,
            resource_name,
            resource_id=runtime_id,
            environment_id=environment_id,
            session_name=f"flowweave-node-{binding_id or 'draft'}",
            working_dir=working_directory,
            rows=rows,
            columns=columns,
        )
        await websocket.accept()

        async def forward_output() -> None:
            assert terminal is not None
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
                    return
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
        reset_settings(token)
