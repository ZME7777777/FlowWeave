from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from flowweave.modules.orchestration import public as service
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.shared.http import Db, IdempotencyKey, command_key, run_sync
from flowweave.shared.schemas import (
    ArtifactWrite,
    AttemptStartWrite,
    AttemptVersionWrite,
    AutomaticRunCopyWrite,
    AutomaticRunDraftUpdateWrite,
    AutomaticRunDraftWrite,
    AutomaticRunStartWrite,
    HumanInputWrite,
    InputBindingsWrite,
    NodeRunStart,
    RejectWrite,
    RunStart,
    RuntimeCancelRecoveryWrite,
    RuntimeConfirmationDecisionWrite,
    RuntimeReplacementWrite,
    SyncSnapshotWrite,
)

router = APIRouter()


def _key(value: str | None, action: str, identifier: str) -> str:
    return command_key(value, fallback=f"{action}:{identifier}:{uuid4()}")


@router.post("/flows/{flow_id}/runs", status_code=201)
async def start_flow(flow_id: str, payload: RunStart, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.start_flow(session, flow_id, payload))


@router.post("/flows/{flow_id}/automatic-runs", status_code=201)
async def create_automatic_run_draft(
    flow_id: str, payload: AutomaticRunDraftWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.create_automatic_run_draft(session, flow_id, payload)
    )


@router.get("/flow-runs/{parent_run_id}/automatic-runs")
async def nested_automatic_runs(parent_run_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: service.list_nested_automatic_runs(session, parent_run_id)
    )


@router.post("/flow-runs/{parent_run_id}/automatic-runs", status_code=201)
async def create_nested_automatic_run(
    parent_run_id: str, payload: AutomaticRunDraftWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.create_nested_automatic_run_draft(
            session, parent_run_id, payload
        ),
    )


@router.put("/flow-runs/{parent_run_id}/automatic-runs/{run_id}")
async def update_nested_automatic_run(
    parent_run_id: str, run_id: str, payload: AutomaticRunDraftUpdateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: (
            service.nested_automatic_run(session, parent_run_id, run_id),
            service.update_automatic_run_draft(session, run_id, payload),
        )[1],
    )


@router.post("/flow-runs/{parent_run_id}/automatic-runs/{run_id}/start")
async def start_nested_automatic_run(
    parent_run_id: str, run_id: str, payload: AutomaticRunStartWrite, db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: (
            service.nested_automatic_run(session, parent_run_id, run_id),
            service.start_automatic_run(
                session, run_id, payload,
                _key(idempotency_key, "start-nested-automatic-run", run_id),
            ),
        )[1],
    )


@router.delete(
    "/flow-runs/{parent_run_id}/automatic-runs/{run_id}",
    status_code=204, response_class=Response,
)
async def delete_nested_automatic_run(
    parent_run_id: str, run_id: str, db: Db
) -> Response:
    await run_sync(
        db,
        lambda session: (
            service.nested_automatic_run(session, parent_run_id, run_id),
            service.delete_run(session, run_id),
        )[1],
    )
    return Response(status_code=204)


@router.put("/automatic-runs/{run_id}")
async def update_automatic_run_draft(
    run_id: str, payload: AutomaticRunDraftUpdateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.update_automatic_run_draft(session, run_id, payload)
    )


@router.post("/automatic-runs/{run_id}/copy", status_code=201)
async def copy_automatic_run_draft(
    run_id: str, payload: AutomaticRunCopyWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.copy_automatic_run_draft(session, run_id, payload)
    )


@router.post("/automatic-runs/{run_id}/start")
async def start_automatic_run(
    run_id: str,
    payload: AutomaticRunStartWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.start_automatic_run(
            session, run_id, payload, _key(idempotency_key, "start-automatic-run", run_id)
        ),
    )


@router.get("/flow-runs")
async def list_flow_runs(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_runs)


@router.get("/flow-runs/{run_id}")
async def flow_run(run_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.run_detail(session, run_id))


@router.get("/flow-runs/{run_id}/runtime")
async def flow_run_runtime(run_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: sandboxes.runtime_overview(session, run_id))


@router.post("/flow-runs/{run_id}/runtime/replacements", status_code=202)
async def replace_flow_run_runtime(
    run_id: str, payload: RuntimeReplacementWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: sandboxes.request_runtime_replacement(
            session,
            run_id,
            expected_generation=payload.expected_generation,
            expected_session_row_version=payload.expected_session_row_version,
        ),
    )


@router.delete("/flow-runs/{run_id}", status_code=204, response_class=Response)
async def delete_flow_run(run_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_run(session, run_id))
    return Response(status_code=204)


@router.get("/flow-runs/{run_id}/nodes/{node_run_id}")
async def node_run(run_id: str, node_run_id: str, db: Db) -> dict[str, Any]:
    result = await run_sync(db, lambda session: service.node_run_detail(session, node_run_id))
    if result["flow_run_id"] != run_id:
        from flowweave.shared.errors import not_found

        raise not_found("node_run", node_run_id)
    return result


@router.post("/flow-runs/{run_id}/artifacts", status_code=201)
async def add_artifact(run_id: str, payload: ArtifactWrite, db: Db) -> dict[str, Any]:
    await db.rollback()
    prepared = await asyncio.to_thread(service.prepare_artifact, payload)
    try:
        return await run_sync(
            db, lambda session: service.create_artifact(session, run_id, prepared)
        )
    except BaseException:
        await asyncio.to_thread(service.discard_prepared_artifacts, [prepared])
        raise


@router.post("/flow-runs/{run_id}/nodes/{flow_node_key}/input-artifacts", status_code=201)
async def add_node_input_artifact(
    run_id: str, flow_node_key: str, payload: ArtifactWrite, db: Db
) -> dict[str, Any]:
    await db.rollback()
    prepared = await asyncio.to_thread(service.prepare_artifact, payload)
    try:
        return await run_sync(
            db,
            lambda session: service.create_node_input_artifact(
                session, run_id, flow_node_key, prepared
            ),
        )
    except BaseException:
        await asyncio.to_thread(service.discard_prepared_artifacts, [prepared])
        raise


@router.post("/flow-runs/{run_id}/artifacts/upload", status_code=201)
async def upload_artifact(
    run_id: str,
    db: Db,
    field_key: Annotated[str, Form(min_length=1, max_length=100)],
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form(max_length=240)] = "",
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    await db.rollback()
    prepared = await asyncio.to_thread(
        service.prepare_file_artifact,
        field_key=field_key,
        filename=file.filename or "attachment",
        mime_type=file.content_type or "application/octet-stream",
        content=content,
        metadata={"source": "HUMAN_INPUT", "display_name": display_name.strip() or field_key},
    )
    try:
        return await run_sync(
            db, lambda session: service.create_artifact(session, run_id, prepared)
        )
    except BaseException:
        await asyncio.to_thread(service.discard_prepared_artifacts, [prepared])
        raise


@router.post("/flow-runs/{run_id}/nodes/{flow_node_key}/input-artifacts/upload", status_code=201)
async def upload_node_input_artifact(
    run_id: str,
    flow_node_key: str,
    db: Db,
    field_key: Annotated[str, Form(min_length=1, max_length=100)],
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form(max_length=240)] = "",
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    await db.rollback()
    prepared = await asyncio.to_thread(
        service.prepare_file_artifact,
        field_key=field_key,
        filename=file.filename or "attachment",
        mime_type=file.content_type or "application/octet-stream",
        content=content,
        metadata={"source": "HUMAN_INPUT", "display_name": display_name.strip() or field_key},
    )
    try:
        return await run_sync(
            db,
            lambda session: service.create_node_input_artifact(
                session, run_id, flow_node_key, prepared
            ),
        )
    except BaseException:
        await asyncio.to_thread(service.discard_prepared_artifacts, [prepared])
        raise


@router.delete(
    "/flow-runs/{run_id}/artifacts/{artifact_id}",
    status_code=204,
    response_class=Response,
)
async def delete_artifact(run_id: str, artifact_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_artifact(session, run_id, artifact_id))
    return Response(status_code=204)


@router.get("/artifact-versions/{artifact_id}/content")
async def artifact_content(artifact_id: str, db: Db, download: bool = False) -> Response:
    reference = await run_sync(
        db, lambda session: service.artifact_content_reference(session, artifact_id)
    )
    content, mime_type, filename = await asyncio.to_thread(service.read_artifact_content, reference)
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/flow-runs/{run_id}/nodes/{flow_node_key}/runs", status_code=201)
async def activate_node(
    run_id: str, flow_node_key: str, payload: NodeRunStart, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.start_node_run(session, run_id, flow_node_key, payload)
    )


@router.put("/node-attempts/{attempt_id}/input-bindings")
async def update_bindings(attempt_id: str, payload: InputBindingsWrite, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.replace_bindings(session, attempt_id, payload)
    )


@router.post("/node-attempts/{attempt_id}/confirm-start")
async def confirm_start(
    attempt_id: str,
    payload: AttemptStartWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.confirm_start(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "confirm-start", attempt_id),
        ),
    )


@router.post("/node-attempts/{attempt_id}/human-input")
async def human_input(
    attempt_id: str,
    payload: HumanInputWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.human_input(
            session, attempt_id, payload, _key(idempotency_key, "human-input", attempt_id)
        ),
    )


@router.post("/node-attempts/{attempt_id}/accept")
async def accept(
    attempt_id: str,
    payload: AttemptVersionWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.accept_attempt(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "accept", attempt_id),
        ),
    )


@router.post("/runtime-confirmation-batches/{batch_id}/decision")
async def decide_runtime_confirmation(
    batch_id: str,
    payload: RuntimeConfirmationDecisionWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.decide_runtime_confirmation(
            session,
            batch_id,
            payload,
            _key(idempotency_key, "runtime-confirmation", batch_id),
        ),
    )


@router.post("/node-attempts/{attempt_id}/reject")
async def reject(
    attempt_id: str,
    payload: RejectWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.reject_attempt(
            session, attempt_id, payload, _key(idempotency_key, "reject", attempt_id)
        ),
    )


@router.post("/node-attempts/{attempt_id}/retry-gates")
async def retry_gates(attempt_id: str, payload: AttemptVersionWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.retry_gates(session, attempt_id, payload))


@router.post("/node-attempts/{attempt_id}/retry-runtime-cancel")
async def retry_runtime_cancel(
    attempt_id: str,
    payload: RuntimeCancelRecoveryWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.retry_runtime_cancel(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "retry-runtime-cancel", attempt_id),
        ),
    )


@router.post("/node-attempts/{attempt_id}/cancel")
async def cancel_attempt(
    attempt_id: str,
    payload: AttemptVersionWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.cancel_attempt(
            session,
            attempt_id,
            payload,
            _key(idempotency_key, "cancel-attempt", attempt_id),
        ),
    )


@router.post("/flow-runs/{run_id}/sync-snapshot")
async def sync_snapshot(
    run_id: str,
    payload: SyncSnapshotWrite,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.sync_snapshot(
            session, run_id, payload, _key(idempotency_key, "sync-snapshot", run_id)
        ),
    )


@router.post("/flow-runs/{run_id}/complete")
async def complete(run_id: str, db: Db, idempotency_key: IdempotencyKey = None) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.complete_run(
            session, run_id, _key(idempotency_key, "complete", run_id)
        ),
    )


@router.post("/flow-runs/{run_id}/cancel")
async def cancel(run_id: str, db: Db, idempotency_key: IdempotencyKey = None) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.cancel_run(
            session, run_id, _key(idempotency_key, "cancel", run_id)
        ),
    )


@router.get("/flow-runs/{run_id}/event-history")
async def event_history(
    run_id: str, db: Db, after: int = Query(default=0, ge=0)
) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.events(session, run_id, after))


@router.get("/flow-runs/{run_id}/events")
def event_stream(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        header_cursor = int(last_event_id or 0)
    except ValueError:
        header_cursor = 0
    cursor = max(after, header_cursor)
    container = request.app.state.container

    async def read_batch(current: int) -> list[dict[str, Any]]:
        def read_events(session: Session) -> list[dict[str, Any]]:
            return service.events(
                session,
                run_id,
                current,
                limit=container.settings.sse_event_batch_size,
            )

        async with container.database.session() as db:
            return await db.run_sync(read_events)

    async def stream():
        nonlocal cursor
        async with container.run_event_listener.subscribe() as subscription:
            while not await request.is_disconnected():
                rows = await read_batch(cursor)
                if rows:
                    for row in rows:
                        cursor = row["cursor"]
                        payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: {row['event_type']}\ndata: {payload}\n\n"
                    # A full batch means backlog may remain. Read again immediately;
                    # generator backpressure bounds memory to this single batch.
                    if len(rows) == container.settings.sse_event_batch_size:
                        await asyncio.sleep(0)
                        continue
                notified = await subscription.wait(
                    run_id,
                    container.settings.sse_heartbeat_seconds,
                )
                if not notified:
                    yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
