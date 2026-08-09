from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect

from flowweave.bootstrap.container import Container
from flowweave.modules.environments.application import service
from flowweave.modules.environments.infrastructure import docker
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, get_container, run_sync
from flowweave.shared.schemas import EnvironmentSetupWrite, TerminalEnvironmentWrite
from flowweave.shared.settings import bind_settings, reset_settings

router = APIRouter()
ContainerDep = Annotated[Container, Depends(get_container)]


@router.get("/terminal-environments")
async def environments(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_environments)


@router.post("/terminal-environments", status_code=201)
async def create_environment(payload: TerminalEnvironmentWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_environment(session, payload))


@router.get("/terminal-environments/{environment_id}")
async def environment(environment_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.read_environment(session, environment_id))


@router.put("/terminal-environments/{environment_id}")
async def update_environment(
    environment_id: str, payload: TerminalEnvironmentWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: service.save_environment(session, payload, environment_id)
    )


@router.delete("/terminal-environments/{environment_id}", status_code=204)
async def delete_environment(environment_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_environment(session, environment_id))
    return Response(status_code=204)


@router.delete("/terminal-environments/{environment_id}/versions/{version_id}", status_code=204)
async def delete_environment_version(environment_id: str, version_id: str, db: Db) -> Response:
    await run_sync(
        db,
        lambda session: service.delete_environment_version(session, environment_id, version_id),
    )
    return Response(status_code=204)


@router.post("/terminal-environments/{environment_id}/setup-sessions", status_code=201)
async def create_setup_session(
    environment_id: str, payload: EnvironmentSetupWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: service.create_setup_session(
            session, environment_id, payload.base_version_id
        ),
    )


@router.post("/environment-setup-sessions/{session_id}/publish", status_code=201)
async def publish_setup_session(session_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.publish_setup_session(session, session_id))


@router.delete("/environment-setup-sessions/{session_id}", status_code=204)
async def stop_setup_session(session_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.stop_setup_session(session, session_id))
    return Response(status_code=204)


@router.websocket("/environment-setup-sessions/{session_id}/terminal")
async def setup_terminal(websocket: WebSocket, session_id: str, container: ContainerDep) -> None:
    settings_token = bind_settings(container.settings)
    master = -1
    process = None
    try:
        async with container.database.session() as db:
            try:
                state, container_id, _ = await db.run_sync(
                    lambda session: service.terminal_session_details(session, session_id)
                )
                await db.commit()
            except DomainError as exc:
                await db.rollback()
                await websocket.close(code=4404, reason=exc.message)
                return
        if state != "RUNNING" or not container_id:
            await websocket.close(code=4409, reason="setup session is not running")
            return

        master, process = await asyncio.to_thread(docker.open_terminal, container_id)
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
                if message.get("bytes") is not None:
                    await asyncio.to_thread(os.write, master, message["bytes"])
                    continue
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
                    data = str(value.get("data", "")).encode()
                    await asyncio.to_thread(os.write, master, data)
        except WebSocketDisconnect:
            pass
        finally:
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 2)
            except TimeoutError:
                process.kill()
        if master >= 0:
            os.close(master)
        reset_settings(settings_token)
