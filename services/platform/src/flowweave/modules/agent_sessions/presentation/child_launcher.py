"""Native-proof-only gateway. Cookie authentication cannot authorize a launch."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError

from flowweave.modules.agent_sessions.application import (
    child_access,
    child_creation,
    child_launcher,
)
from flowweave.modules.agent_sessions.application.child_parent import LAUNCH_PATH, denied
from flowweave.runtime.child_creation import NativeChildLaunchRequest
from flowweave.runtime.child_interaction import NativeCommandRequest, NativeQueryRequest
from flowweave.shared.errors import DomainError
from flowweave.shared.http import get_container, run_formal_events

router = APIRouter()
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PATH = re.compile(rf"^{LAUNCH_PATH}/{_UUID}/{_UUID}/(?:start|queries|commands)$")


def native_launcher_path(path: str) -> bool:
    return _PATH.fullmatch(path) is not None


@router.post(LAUNCH_PATH + "/{owner_id}/{binding_id}/start")
async def start(owner_id: UUID, binding_id: UUID, request: Request) -> JSONResponse:
    header = request.headers.get("X-Session-API-Key", "")
    if not header or len(header) > 256:
        raise denied()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 262_144:
            raise DomainError("AGENT_CHILD_REQUEST_TOO_LARGE", "子会话任务超出请求上限", 413)
    try:
        launch = NativeChildLaunchRequest.model_validate_json(body)
    except ValidationError:
        # The shared validation handler would echo the opaque proof and task.
        raise DomainError("AGENT_CHILD_REQUEST_INVALID", "子会话请求格式无效", 422) from None
    key = SecretStr(header)
    container = get_container(request)
    releasing = False
    try:
        reserved = await run_formal_events(
            container,
            lambda db: child_launcher.prepare_launcher(
                db, owner_id=owner_id, binding_id=binding_id, runtime_key=key
            ),
            lambda parent: child_creation.read_creation(parent, launch),
            lambda db, parent, verified: child_creation.reserve_creation(
                db, parent, verified, runtime_key=key
            ),
            history=False,
        )
        if isinstance(reserved, child_launcher.AuthorizedParent):
            request.state.audit_principal = reserved.principal
            raise child_creation.unavailable()
        request.state.audit_principal = reserved.parent.principal
        governed = await run_formal_events(
            container,
            lambda db: child_creation.reconcile_reservation(db, reserved, runtime_key=key),
            lambda child: child_creation.prepare_native(child, launch),
            lambda db, child, prepared: child_creation.govern_creation(
                db, child, prepared, runtime_key=key
            ),
            history=False,
        )
        releasing = True
        result = await run_formal_events(
            container,
            lambda db: child_creation.reconcile_reservation(db, governed, runtime_key=key),
            lambda child: child_creation.release_native(child, launch),
            lambda db, child, prepared: child_creation.finish_creation(
                db, child, prepared, runtime_key=key
            ),
            history=False,
        )
    except DomainError as exc:
        if (
            exc.details.get("outcome_unknown") is True
            or exc.code == "RUNTIME_BUSINESS_READ_TIMEOUT"
            or (releasing and exc.code == "AGENT_CHILD_LAUNCH_DENIED")
        ):
            raise DomainError(
                "AGENT_CHILD_CREATION_AMBIGUOUS",
                "子会话创建结果待对账，请保留原创建身份",
                504,
                {"outcome_unknown": True},
            ) from None
        raise
    return JSONResponse(result)


async def access(
    owner_id: UUID, binding_id: UUID, request: Request, *, command: bool
) -> JSONResponse:
    header = request.headers.get("X-Session-API-Key", "")
    if not header or len(header) > 256:
        raise denied()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 262144:
            raise DomainError("AGENT_CHILD_REQUEST_TOO_LARGE", "子会话请求超出上限", 413)
    try:
        payload = (
            NativeCommandRequest.model_validate_json(body)
            if command
            else NativeQueryRequest.model_validate_json(body)
        )
    except ValidationError:
        raise DomainError("AGENT_CHILD_REQUEST_INVALID", "子会话请求格式无效", 422) from None
    key = SecretStr(header)
    container = get_container(request)
    verified = await run_formal_events(
        container,
        lambda db: child_launcher.prepare_launcher(
            db, owner_id=owner_id, binding_id=binding_id, runtime_key=key
        ),
        lambda parent: child_access.verify(parent, payload),
        lambda db, parent, claim: child_access.authorize(db, parent, claim, key),
        history=False,
    )
    request.state.audit_principal = verified.parent.principal
    try:
        result = await run_formal_events(
            container,
            lambda db: child_access.authorize(db, verified.parent, verified.claim, key),
            lambda allowed: child_access.execute(allowed, payload),
            lambda db, allowed, value: child_access.project(db, allowed, value, key),
            history=not command,
            timeout_seconds=35 if not command else None,
        )
    except DomainError as exc:
        if command and (exc.status >= 500 or exc.code == "AGENT_CHILD_LAUNCH_DENIED"):
            raise DomainError(
                "AGENT_CHILD_COMMAND_AMBIGUOUS",
                "子指令结果待对账，请保留原调用身份",
                504,
                {"outcome_unknown": True},
            ) from None
        raise
    return JSONResponse(result)


@router.post(LAUNCH_PATH + "/{owner_id}/{binding_id}/queries")
async def queries(owner_id: UUID, binding_id: UUID, request: Request) -> JSONResponse:
    return await access(owner_id, binding_id, request, command=False)


@router.post(LAUNCH_PATH + "/{owner_id}/{binding_id}/commands")
async def commands(owner_id: UUID, binding_id: UUID, request: Request) -> JSONResponse:
    return await access(owner_id, binding_id, request, command=True)
