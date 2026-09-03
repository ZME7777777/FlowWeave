from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

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
from flowweave.modules.agent_sessions.public import conversations
from flowweave.modules.agent_workspaces.application import work_directories, workspace
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


class AgentWorkspaceCapabilitiesWrite(_Write):
    capability_version_ids: list[str] = Field(default_factory=list)


AgentWorkDirectoryPathWrite = Annotated[str, Field(min_length=1, max_length=500)]


class AgentWorkDirectoryCreateWrite(_Write):
    display_name: str = Field(min_length=1, max_length=160)
    selected_paths: list[AgentWorkDirectoryPathWrite] = Field(min_length=1, max_length=20)


class AgentWorkDirectoryPatchWrite(_Write):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    selected_paths: list[AgentWorkDirectoryPathWrite] | None = Field(
        default=None, min_length=1, max_length=20
    )


class AgentConversationPatchWrite(_Write):
    title: str = Field(min_length=1, max_length=200)


class AgentConversationCapabilityAddWrite(_Write):
    capability_version_id: str = Field(min_length=1, max_length=36)


class AgentAttachmentReference(_Write):
    path: str = Field(min_length=1, max_length=300)
    image_data_url: str | None = Field(default=None, max_length=35_000_000)
    filename: str | None = Field(default=None, max_length=240)
    mime_type: str | None = Field(default=None, max_length=200)
    byte_size: int | None = Field(default=None, ge=0, le=25 * 1024 * 1024)


def _empty_attachment_references() -> list[AgentAttachmentReference]:
    return []


class AgentConversationBootstrapWrite(_Write):
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    work_directory_id: str | None = Field(default=None, min_length=1, max_length=36)
    content: str = Field(max_length=200_000)
    attachments: list[AgentAttachmentReference] = Field(
        default_factory=_empty_attachment_references, max_length=10
    )
    capability_version_ids: list[str] = Field(default_factory=list)


class AgentMessageWrite(_Write):
    content: str = Field(max_length=200_000)
    attachments: list[AgentAttachmentReference] = Field(
        default_factory=_empty_attachment_references, max_length=10
    )


class AgentConversationModelWrite(_Write):
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)


class AgentConversationForkWrite(_Write):
    event_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=240)


class AgentStreamingMigrationWrite(_Write):
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str | None = Field(default=None, min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)


class AgentConfirmationDecisionWrite(_Write):
    expected_pending_digest: str = Field(min_length=1, max_length=128)
    accept: bool
    reason: str = Field(min_length=1, max_length=2_000)


def _terminal_instance_id(value: str | None) -> str:
    try:
        return str(UUID(value or ""))
    except ValueError as exc:
        raise DomainError("AGENT_TERMINAL_ID_INVALID", "终端实例标识无效", 422) from exc


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


async def _forward_runtime_events(websocket: WebSocket, runtime: Any, handle: Any) -> None:
    """Forward one transient Runtime stream while actively observing disconnects.

    An idle Runtime event stream has no writes through which ``send_json`` can
    notice a browser refresh.  Keep a receive pending as well, so closing the
    WebSocket also closes the upstream async generator and its Provider relay.
    """

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


@router.get("/agent-workspaces/{workspace_id}/capabilities")
async def get_agent_workspace_capabilities(workspace_id: str, db: Db) -> list[dict[str, str]]:
    return await run_sync(
        db, lambda session: conversations.workspace_capabilities(session, workspace_id)
    )


@router.put("/agent-workspaces/{workspace_id}/capabilities")
async def put_agent_workspace_capabilities(
    workspace_id: str, payload: AgentWorkspaceCapabilitiesWrite, db: Db
) -> list[dict[str, str]]:
    return await run_sync(
        db,
        lambda session: conversations.replace_workspace_capabilities(
            session, workspace_id, tuple(payload.capability_version_ids)
        ),
    )


@router.post("/agent-workspaces/{workspace_id}/capabilities/{capability_version_id}/mcp-readiness")
async def probe_agent_workspace_mcp_readiness(
    workspace_id: str, capability_version_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.probe_workspace_mcp_readiness(
            session, workspace_id, capability_version_id
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/runtime")
async def get_agent_workspace_runtime(workspace_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: conversations.runtime_status(session, workspace_id))


@router.get("/agent-workspaces/{workspace_id}/work-directories")
async def list_agent_work_directories(workspace_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: work_directories.list_work_directories(session, workspace_id)
    )


@router.get("/agent-workspaces/{workspace_id}/workspace")
async def get_agent_workspace_details(
    workspace_id: str,
    db: Db,
    work_directory_id: str | None = Query(default=None),
    binding_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: workspace.details(
            session,
            workspace_id,
            work_directory_id=work_directory_id,
            binding_id=binding_id,
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/workspace/file")
async def download_agent_workspace_file(
    workspace_id: str,
    db: Db,
    path: str = Query(...),
    binding_id: str | None = Query(default=None),
    work_directory_id: str | None = Query(default=None),
    download: bool = Query(default=False),
) -> Response:
    item = await run_sync(
        db,
        lambda session: workspace.download(
            session, workspace_id, path, binding_id, work_directory_id
        ),
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content=item.content,
        media_type=item.content_type,
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.filename)}"},
    )


@router.delete("/agent-workspaces/{workspace_id}/workspace/file", status_code=204)
async def delete_agent_workspace_file(
    workspace_id: str,
    db: Db,
    path: str = Query(...),
    binding_id: str | None = Query(default=None),
    work_directory_id: str | None = Query(default=None),
) -> Response:
    await run_sync(
        db,
        lambda session: workspace.delete_entry(
            session, workspace_id, path, binding_id, work_directory_id
        ),
    )
    return Response(status_code=204)


@router.post("/agent-workspaces/{workspace_id}/work-directories", status_code=201)
async def create_agent_work_directory(
    workspace_id: str, payload: AgentWorkDirectoryCreateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: work_directories.create_work_directory(
            session,
            workspace_id,
            payload.display_name,
            tuple(payload.selected_paths),
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}")
async def get_agent_work_directory(
    workspace_id: str, work_directory_id: str, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: work_directories.get_work_directory(
            session, workspace_id, work_directory_id
        ),
    )


@router.patch("/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}")
async def patch_agent_work_directory(
    workspace_id: str,
    work_directory_id: str,
    payload: AgentWorkDirectoryPatchWrite,
    db: Db,
) -> dict[str, Any]:
    if payload.display_name is None and payload.selected_paths is None:
        raise DomainError("AGENT_WORK_DIRECTORY_PATCH_EMPTY", "工作目录修改内容不能为空", 422)
    return await run_sync(
        db,
        lambda session: work_directories.update_work_directory(
            session,
            workspace_id,
            work_directory_id,
            display_name=payload.display_name,
            selected_paths=(
                tuple(payload.selected_paths) if payload.selected_paths is not None else None
            ),
        ),
    )


@router.delete(
    "/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}",
    status_code=204,
    response_class=Response,
)
async def delete_agent_work_directory(
    workspace_id: str, work_directory_id: str, db: Db
) -> Response:
    await run_sync(
        db,
        lambda session: work_directories.delete_work_directory(
            session, workspace_id, work_directory_id
        ),
    )
    return Response(status_code=204)


@router.get("/agent-workspaces/{workspace_id}/conversations")
async def list_agent_conversations(workspace_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: conversations.list_conversations(session, workspace_id)
    )


@router.post("/agent-workspaces/{workspace_id}/conversations", status_code=201)
async def create_agent_conversation(
    workspace_id: str,
    payload: AgentConversationBootstrapWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        raise DomainError(
            "AGENT_BOOTSTRAP_IDEMPOTENCY_KEY_REQUIRED",
            "首条消息必须携带幂等请求标识",
            422,
        )
    return await run_sync(
        db,
        lambda session: conversations.bootstrap_conversation(
            session,
            workspace_id,
            work_directory_id=payload.work_directory_id,
            conversation_id=payload.conversation_id,
            model_provider_id=payload.model_provider_id,
            model_name=payload.model_name,
            reasoning_effort=payload.reasoning_effort,
            content=payload.content,
            attachments=tuple(item.model_dump(exclude_none=True) for item in payload.attachments),
            capability_version_ids=tuple(payload.capability_version_ids),
            idempotency_key=idempotency_key,
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


@router.post("/agent-workspaces/{workspace_id}/conversations/{binding_id}/capabilities")
async def add_agent_conversation_capability(
    workspace_id: str,
    binding_id: str,
    payload: AgentConversationCapabilityAddWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.add_conversation_capability(
            session, workspace_id, binding_id, payload.capability_version_id
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


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}/pending-confirmation")
async def agent_pending_confirmation(workspace_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.pending_confirmation(session, workspace_id, binding_id),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/pending-confirmation/decision",
    status_code=202,
)
async def agent_confirmation_decision(
    workspace_id: str,
    binding_id: str,
    payload: AgentConfirmationDecisionWrite,
    db: Db,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.decide_confirmation(
            session,
            workspace_id,
            binding_id,
            expected_pending_digest=payload.expected_pending_digest,
            accept=payload.accept,
            reason=payload.reason,
        ),
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
            session,
            workspace_id,
            binding_id,
            payload.content,
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


@router.post("/agent-workspaces/{workspace_id}/attachments", status_code=201)
async def agent_workspace_attachment(
    workspace_id: str,
    db: Db,
    file: Annotated[UploadFile, File()],
    work_directory_id: str | None = Query(default=None),
    conversation_id: str | None = Query(default=None, min_length=36, max_length=36),
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    return await run_sync(
        db,
        lambda session: conversations.upload_attachment(
            session,
            workspace_id,
            None,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
            work_directory_id=work_directory_id,
            attachment_owner_id=conversation_id,
        ),
    )


@router.get("/agent-workspaces/{workspace_id}/conversations/{binding_id}/context")
async def agent_context(
    workspace_id: str, binding_id: str, db: Db
) -> dict[str, int | float | str | bool | None]:
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
            payload.model_provider_id,
            payload.model_name,
            payload.reasoning_effort,
        ),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/streaming-migration",
    status_code=201,
)
async def agent_streaming_migration(
    workspace_id: str,
    binding_id: str,
    payload: AgentStreamingMigrationWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: conversations.migrate_streaming_conversation(
            session,
            workspace_id,
            binding_id,
            payload.model_provider_id,
            payload.model_name,
            payload.reasoning_effort,
            _key(idempotency_key, "migrate-agent-streaming", binding_id),
        ),
    )


@router.post(
    "/agent-workspaces/{workspace_id}/conversations/{binding_id}/condense", status_code=202
)
async def agent_condense_conversation(workspace_id: str, binding_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: conversations.condense_conversation(session, workspace_id, binding_id)
    )


@router.post("/agent-workspaces/{workspace_id}/conversations/{binding_id}/fork", status_code=201)
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
async def agent_input_readiness(
    workspace_id: str, binding_id: str, db: Db
) -> dict[str, bool | str]:
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
            await _forward_runtime_events(websocket, runtime, handle)
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
        try:
            terminal_instance_id = _terminal_instance_id(
                websocket.query_params.get("terminal_instance_id")
            )
        except DomainError as exc:
            await websocket.close(code=4409, reason=exc.message)
            return
        async with container.database.session() as db:
            try:
                resource_name, runtime_id, working_directory, container_id = await db.run_sync(
                    lambda session: workspace.terminal_details(
                        session,
                        workspace_id,
                        work_directory_id=websocket.query_params.get("work_directory_id") or None,
                        binding_id=websocket.query_params.get("binding_id") or None,
                    )
                )
            except DomainError as exc:
                await websocket.close(code=4409, reason=exc.message)
                return
        session_name = workspace.terminal_session_name(
            workspace_id, container_id, terminal_instance_id
        )
        try:
            terminal = await asyncio.to_thread(
                environments.open_managed_terminal,
                resource_name,
                resource_id=runtime_id,
                session_name=session_name,
                working_dir=working_directory,
                rows=rows,
                columns=columns,
            )
        except DomainError as exc:
            await websocket.close(code=4409, reason=exc.message)
            return
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


@router.delete("/agent-workspaces/{workspace_id}/terminals/{terminal_instance_id}", status_code=204)
async def close_agent_workspace_terminal(
    workspace_id: str,
    terminal_instance_id: str,
    container: ContainerDep,
    work_directory_id: str | None = Query(default=None),
    binding_id: str | None = Query(default=None),
) -> Response:
    instance_id = _terminal_instance_id(terminal_instance_id)
    settings_token = bind_settings(container.settings)
    try:
        async with container.database.session() as db:
            resource_name, runtime_id, _, container_id = await db.run_sync(
                lambda session: workspace.terminal_details(
                    session,
                    workspace_id,
                    work_directory_id=work_directory_id,
                    binding_id=binding_id,
                )
            )
        session_name = workspace.terminal_session_name(workspace_id, container_id, instance_id)
        await asyncio.to_thread(
            environments.destroy_managed_terminal_session,
            resource_name,
            resource_id=runtime_id,
            session_name=session_name,
        )
        return Response(status_code=204)
    finally:
        reset_settings(settings_token)
