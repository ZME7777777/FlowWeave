"""Cookie-authenticated, parent-scoped child surface shared by both product hosts."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.bootstrap.container import Container
from flowweave.modules.agent_sessions.application import child_activity, credential_sync
from flowweave.modules.agent_sessions.application import child_sessions as children
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.credentials.application.service import (
    credential_secret_descriptions,
    resolve_credentials_for_agent,
)
from flowweave.modules.model_providers.public import has_connected_default_model
from flowweave.runtime.base import RuntimeEventBatch, RuntimeProvider
from flowweave.runtime.dependencies import get_runtime, runtime_context
from flowweave.runtime.request import runtime_provider
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError
from flowweave.shared.http import (
    IdempotencyKey,
    command_key,
    get_container,
    run_blocking_history,
    run_blocking_mutation,
    run_formal_events,
)
from flowweave.shared.runtime_stream import forward_runtime_stream
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter(prefix="/agent-session-children/{parent_id}")
child_router = APIRouter(prefix="/{child_id}")
ContainerDep = Annotated[Container, Depends(get_container)]


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttachmentReference(Write):
    path: str = Field(min_length=1, max_length=1000)
    filename: str | None = Field(default=None, max_length=240)
    mime_type: str | None = Field(default=None, max_length=200)
    byte_size: int | None = Field(default=None, ge=0, le=25 * 1024 * 1024)
    image_data_url: str | None = Field(default=None, max_length=35000000)


class EventReference(Write):
    source_binding_id: UUID
    source_event_id: str = Field(min_length=1, max_length=200)
    selection: dict[str, int] | None = None


class MessageWrite(Write):
    submission_id: UUID
    content: str = Field(max_length=200000)
    attachments: list[AttachmentReference] = Field(default_factory=lambda: [], max_length=10)
    references: list[EventReference] = Field(default_factory=lambda: [], max_length=10)
    workspace_references: list[dict[str, Any]] = Field(default_factory=lambda: [], max_length=20)
    annotations: list[dict[str, Any]] = Field(default_factory=lambda: [], max_length=20)


class ControlWrite(Write):
    operation: Literal["interrupt", "resume", "condense"]


class ModelWrite(Write):
    model_provider_id: str = Field(min_length=1, max_length=36)
    model_name: str = Field(min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)


class CredentialsWrite(Write):
    credential_ids: list[UUID] = Field(max_length=200)


class UnreadWrite(Write):
    unread: bool
    unread_origin: Literal["MANUAL", "SYSTEM"] | None = None


@router.get("")
async def child_list(parent_id: str, container: ContainerDep) -> list[dict[str, Any]]:
    return await run_blocking_history(
        container, lambda db: child_activity.list_children(db, parent_id)
    )


@child_router.put("/unread")
async def unread(
    parent_id: str, child_id: str, payload: UnreadWrite, container: ContainerDep
) -> dict[str, Any]:
    return await run_blocking_mutation(
        container,
        lambda db: child_activity.set_unread(
            db, parent_id, child_id, unread=payload.unread, origin=payload.unread_origin
        ),
    )


@child_router.get("")
async def details(parent_id: str, child_id: str, container: ContainerDep) -> dict[str, Any]:
    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        lambda loc: children.verify_native(loc),
        lambda db, loc, _: children.view(db, loc),
        history=False,
    )


@child_router.get("/events")
async def events(
    parent_id: str,
    child_id: str,
    container: ContainerDep,
    cursor: str | None = Query(default=None, max_length=200),
    history_cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        lambda loc: children.read(loc, cursor, history_cursor),
        children.project_events,
        history=bool(history_cursor and not cursor),
    )


@child_router.get("/hydration")
async def hydration(
    parent_id: str,
    child_id: str,
    container: ContainerDep,
    cursor: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    def project(
        db: Session, loc: children.ChildLocator, batch: RuntimeEventBatch
    ) -> dict[str, Any]:
        events = children.project_events(db, loc, batch)
        return {
            "events": events,
            "context": batch.context or {},
            "readiness": batch.readiness.as_dict() if batch.readiness else None,
            **({"incremental_from": cursor} if cursor and batch.cursor_anchor_found else {}),
        }

    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        lambda loc: children.read_hydration(loc, cursor),
        project,
        history=False,
    )


@child_router.websocket("/stream")
async def stream(
    websocket: WebSocket,
    parent_id: str,
    child_id: str,
    container: ContainerDep,
    after_seq: int | None = Query(default=None, ge=-1, le=9_007_199_254_740_991),
) -> None:
    token = bind_settings(container.settings)
    accepted = False
    try:
        with runtime_context(container.runtime):
            locator = await run_formal_events(
                container,
                lambda db: children.resolve(db, parent_id, child_id),
                lambda loc: children.verify_native(loc),
                lambda db, loc, _: (children.revalidate(db, loc), loc)[1],
                history=False,
            )

            async def revalidate() -> None:
                await run_blocking_history(container, lambda db: children.revalidate(db, locator))

            async def project(event: dict[str, Any]) -> dict[str, Any] | None:
                if event.get("type") == "delta":
                    return None
                await revalidate()
                return event

            runtime = runtime_for("openhands", locator.handle)
            await websocket.accept()
            accepted = True
            await forward_runtime_stream(
                websocket,
                runtime.stream_events(locator.handle, after_seq=after_seq),
                project,
                revalidate,
            )
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except DomainError:
        await websocket.close(code=4409, reason="子会话连接已失效")
    except Exception:
        await websocket.close(code=1011 if accepted else 4409, reason="子会话连接暂不可用")
    finally:
        reset_settings(token)


@child_router.get("/input-readiness")
async def readiness(parent_id: str, child_id: str, container: ContainerDep) -> dict[str, Any]:
    def read(loc: children.ChildLocator):
        children.verify_native(loc)
        return get_runtime().input_readiness(loc.handle).as_dict()

    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        read,
        lambda db, loc, value: (children.revalidate(db, loc), value)[1],
        history=False,
    )


@child_router.get("/context")
async def context(parent_id: str, child_id: str, container: ContainerDep) -> dict[str, Any]:
    def read(loc: children.ChildLocator):
        children.verify_native(loc)
        return get_runtime().conversation_context(loc.handle)

    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        read,
        lambda db, loc, value: (children.revalidate(db, loc), value)[1],
        history=False,
    )


@child_router.post("/messages", status_code=202)
async def message(
    parent_id: str, child_id: str, payload: MessageWrite, container: ContainerDep
) -> dict[str, object]:
    attachments = tuple(item.model_dump(exclude_none=True) for item in payload.attachments)
    references = tuple(
        item.model_dump(mode="json", exclude_none=True) for item in payload.references
    )
    workspace_references = tuple(payload.workspace_references)
    annotations = tuple(payload.annotations)

    def prepare(db: Session) -> children.ChildLocator:
        loc = children.resolve(db, parent_id, child_id, write=True)
        children.validate_message(
            db, loc, payload.content, attachments, references, workspace_references, annotations
        )
        return loc

    return await run_formal_events(
        container,
        prepare,
        lambda loc: children.send(
            loc,
            payload.submission_id,
            payload.content,
            attachments,
            references,
            workspace_references,
            annotations,
        ),
        lambda db, loc, event: children.project_send(db, loc, event, payload.content, attachments),
        history=False,
    )


@child_router.post("/control", status_code=202)
async def control(
    parent_id: str,
    child_id: str,
    payload: ControlWrite,
    container: ContainerDep,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, object]:
    if payload.operation == "condense":
        from flowweave.modules.agent_sessions.application.condensation import (
            enqueue_manual_condensation,
        )

        def prepare(db: Session) -> children.ChildLocator:
            return children.resolve(db, parent_id, child_id, write=True)

        def inspect(loc: children.ChildLocator) -> None:
            children.verify_native(loc)
            if not get_runtime().can_accept_input(loc.handle):
                raise DomainError("AGENT_CONVERSATION_BUSY", "请在回复完成或暂停后压缩上下文", 409)

        def accept(db: Session, loc: children.ChildLocator, _: object) -> dict[str, object]:
            binding = children.revalidate(db, loc, write=True)
            db.scalar(
                select(AgentConversationBinding)
                .where(AgentConversationBinding.id == binding.id)
                .with_for_update()
            )
            request_key = command_key(idempotency_key, fallback=str(uuid4()))
            task = enqueue_manual_condensation(
                db,
                binding_id=loc.binding_id,
                idempotency_key=f"condense-child:{loc.binding_id}:{request_key}",
            )
            return {"accepted": True, "task_id": task.id}

        return await run_formal_events(container, prepare, inspect, accept, history=False)
    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id, write=True),
        lambda loc: children.control(loc, payload.operation),
        lambda db, loc, value: (children.mutation_binding(db, loc), value)[1],
        history=False,
    )


@child_router.post("/model")
async def model(
    parent_id: str, child_id: str, payload: ModelWrite, container: ContainerDep
) -> dict[str, object]:
    def prepare(db: Session) -> tuple[children.ChildLocator, RuntimeProvider]:
        loc = children.resolve(db, parent_id, child_id, write=True)
        if not has_connected_default_model(db, payload.model_provider_id):
            raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "请选择可用的模型供应商", 409)
        provider = runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": payload.model_provider_id}}},
            model_name=payload.model_name,
            reasoning_effort=payload.reasoning_effort,
        )
        return loc, provider

    def project(
        db: Session, prepared: tuple[children.ChildLocator, RuntimeProvider], _: object
    ) -> dict[str, object]:
        loc, provider = prepared
        binding = children.mutation_binding(db, loc)
        binding.model_provider_id, binding.model_name, binding.reasoning_effort = (
            provider.provider_id,
            provider.model,
            provider.reasoning_effort,
        )
        db.flush()
        return {
            "model_provider_id": provider.provider_id,
            "model_name": provider.model,
            "reasoning_effort": provider.reasoning_effort,
        }

    return await run_formal_events(
        container,
        prepare,
        lambda prepared: children.control(prepared[0], "resume", prepared[1]),
        project,
        history=False,
    )


@child_router.post("/attachments", status_code=201)
async def upload(
    parent_id: str, child_id: str, container: ContainerDep, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id, write=True),
        lambda loc: children.upload(
            loc, file.filename or "", file.content_type or "application/octet-stream", content
        ),
        children.project_upload,
        history=False,
    )


@child_router.delete("/attachments", status_code=204)
async def delete_attachment(
    parent_id: str, child_id: str, container: ContainerDep, path: str = Query(max_length=1000)
) -> Response:
    await run_blocking_mutation(
        container,
        lambda db: children.remove_pending_attachment(
            db, children.resolve(db, parent_id, child_id, write=True), path
        ),
    )
    return Response(status_code=204)


@child_router.get("/file")
async def file(
    parent_id: str, child_id: str, container: ContainerDep, path: str = Query(max_length=1000)
) -> Response:
    def prepare(db: Session) -> children.ChildLocator:
        loc = children.resolve(db, parent_id, child_id)
        children.validate_file(db, loc, path)
        return loc

    value = await run_formal_events(
        container,
        prepare,
        lambda loc: children.download(loc, path),
        lambda db, loc, result: (
            children.revalidate(db, loc),
            children.validate_file(db, loc, path),
            result,
        )[2],
        history=False,
    )
    return Response(
        content=value.content,
        media_type=value.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@child_router.get("/result-reference/{event_id}")
async def reference(
    parent_id: str, child_id: str, event_id: str, container: ContainerDep
) -> dict[str, str]:
    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        lambda loc: children.result_reference(loc, event_id),
        lambda db, loc, value: (children.revalidate(db, loc), value)[1],
        history=False,
    )


@child_router.get("/credentials")
async def credentials(parent_id: str, child_id: str, container: ContainerDep) -> dict[str, Any]:
    def read(db: Session) -> dict[str, Any]:
        loc = children.resolve(db, parent_id, child_id)
        return credential_sync.list_credential_sync_state(db, children.revalidate(db, loc))

    return await run_blocking_mutation(container, read)


@child_router.post("/credentials/sync")
async def sync_credentials(
    parent_id: str, child_id: str, payload: CredentialsWrite, container: ContainerDep
) -> dict[str, Any]:
    def prepare(db: Session) -> tuple[children.ChildLocator, dict[str, str], list[dict[str, Any]]]:
        loc = children.resolve(db, parent_id, child_id, write=True)
        values, _, summaries = resolve_credentials_for_agent(
            db, tuple(str(item) for item in payload.credential_ids)
        )
        return loc, values, summaries

    def write(prepared: tuple[children.ChildLocator, dict[str, str], list[dict[str, Any]]]) -> None:
        loc, values, summaries = prepared
        children.verify_native(loc)
        get_runtime().update_conversation_secrets(
            loc.handle, values, descriptions=credential_secret_descriptions(summaries)
        )

    def project(
        db: Session,
        prepared: tuple[children.ChildLocator, dict[str, str], list[dict[str, Any]]],
        _: object,
    ) -> dict[str, Any]:
        loc, _, summaries = prepared
        binding = children.mutation_binding(db, loc)
        credential_sync.record_credential_versions(db, binding, summaries)
        return credential_sync.list_credential_sync_state(db, binding)

    return await run_formal_events(container, prepare, write, project, history=False)


class ConfirmationWrite(Write):
    expected_pending_digest: str = Field(min_length=1, max_length=200)
    accept: bool
    reason: str = Field(min_length=1, max_length=2000)


@child_router.get("/pending-confirmation")
async def pending_confirmation(
    parent_id: str, child_id: str, container: ContainerDep
) -> dict[str, Any]:
    from dataclasses import asdict

    def read(loc: children.ChildLocator) -> dict[str, Any]:
        children.verify_native(loc)
        pending = get_runtime().get_pending_confirmation(loc.handle)
        return {"pending": False} if pending is None else {"pending": True, **asdict(pending)}

    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id),
        read,
        lambda db, loc, value: (children.revalidate(db, loc), value)[1],
        history=False,
    )


@child_router.post("/pending-confirmation/decision", status_code=202)
async def confirm(
    parent_id: str, child_id: str, payload: ConfirmationWrite, container: ContainerDep
) -> dict[str, object]:
    if not payload.reason.strip():
        raise DomainError("AGENT_CONFIRMATION_REASON_REQUIRED", "请填写确认理由", 422)

    def write(loc: children.ChildLocator) -> dict[str, object]:
        children.verify_native(loc)
        result = get_runtime().respond_to_confirmation(
            loc.handle, payload.expected_pending_digest, payload.accept, payload.reason.strip()
        )
        return {"accepted": True, "cursor": result.cursor}

    return await run_formal_events(
        container,
        lambda db: children.resolve(db, parent_id, child_id, write=True),
        write,
        lambda db, loc, value: (children.mutation_binding(db, loc), value)[1],
        history=False,
    )


router.include_router(child_router)
