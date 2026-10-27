"""Native-proof-only gateway. Cookie authentication cannot authorize a launch."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError

from flowweave.modules.agent_sessions.application import child_launcher
from flowweave.modules.agent_sessions.application.child_parent import LAUNCH_PATH, denied
from flowweave.runtime.child_creation import NativeChildLaunchRequest
from flowweave.shared.errors import DomainError
from flowweave.shared.http import get_container, run_formal_events

router = APIRouter()
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PATH = re.compile(rf"^{LAUNCH_PATH}/{_UUID}/{_UUID}/start$")


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
    prepared = await run_formal_events(
        get_container(request),
        lambda db: child_launcher.prepare_launcher(
            db, owner_id=owner_id, binding_id=binding_id, runtime_key=key
        ),
        lambda parent: child_launcher.verify_launcher(parent, launch),
        lambda db, parent, invocation: child_launcher.project_launcher(
            db, parent, invocation, runtime_key=key
        ),
        history=False,
    )
    request.state.audit_principal = prepared.principal
    # FR-723 will consume this authorized boundary for reservation and release.
    # Never return a native invocation as a fabricated child creation response.
    raise DomainError(
        "AGENT_CHILD_CREATION_NOT_ENABLED",
        "子会话创建治理尚未启用",
        503,
        {"outcome_unknown": False},
    )
