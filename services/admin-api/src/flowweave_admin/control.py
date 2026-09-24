from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from flowweave_admin.settings import Settings


class RuntimeReplacementCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_run_id: str = Field(min_length=36, max_length=36)
    runtime_session_id: str = Field(min_length=36, max_length=36)
    expected_generation: int = Field(ge=1)
    expected_session_row_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=200)




class AlertLifecycleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_key: str = Field(min_length=3, max_length=300)
    action: str = Field(pattern=r"^(ACKNOWLEDGE|SILENCE)$")
    reason: str = Field(min_length=10, max_length=500)
    silence_minutes: int | None = Field(default=None, ge=5, le=1_440)

class AdminControlError(RuntimeError):
    def __init__(self, *, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


async def request_runtime_replacement(
    settings: Settings,
    command: RuntimeReplacementCommand,
    *,
    actor_user_id: str,
    actor_username: str,
    request_id: str,
) -> dict[str, Any]:
    if not settings.admin_control_api_key:
        raise AdminControlError(
            status=503,
            code="ADMIN_CONTROL_UNAVAILABLE",
            message="Administrator Runtime controls are not configured",
        )
    payload = command.model_dump()
    payload.update(
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        request_id=request_id,
    )
    try:
        async with httpx.AsyncClient(timeout=settings.admin_api_request_timeout_seconds) as client:
            response = await client.post(
                f"{settings.platform_api_url.rstrip('/')}/internal/admin-control/runtime-replacements",
                json=payload,
                headers={"X-FlowWeave-Admin-Control-Key": settings.admin_control_api_key},
            )
    except httpx.HTTPError as exc:
        raise AdminControlError(
            status=503,
            code="ADMIN_CONTROL_UNAVAILABLE",
            message="The platform Runtime control service is unavailable",
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise AdminControlError(
            status=502,
            code="ADMIN_CONTROL_PROTOCOL_ERROR",
            message="The platform Runtime control service returned invalid data",
        ) from exc
    if response.is_error:
        error = body.get("error") if isinstance(body, dict) else None
        detail = error if isinstance(error, dict) else {}
        raise AdminControlError(
            status=response.status_code,
            code=str(detail.get("code") or "ADMIN_CONTROL_FAILED"),
            message=str(detail.get("message") or "The Runtime replacement was rejected"),
        )
    if not isinstance(body, dict):
        raise AdminControlError(
            status=502,
            code="ADMIN_CONTROL_PROTOCOL_ERROR",
            message="The platform Runtime control service returned invalid data",
        )
    return body


async def update_alert_lifecycle(
    settings: Settings,
    command: AlertLifecycleCommand,
    *,
    actor_user_id: str,
    actor_username: str,
    request_id: str,
) -> dict[str, Any]:
    if not settings.admin_control_api_key:
        raise AdminControlError(
            status=503,
            code="ADMIN_CONTROL_UNAVAILABLE",
            message="Administrator alert controls are not configured",
        )
    payload = command.model_dump()
    payload.update(
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        request_id=request_id,
    )
    try:
        async with httpx.AsyncClient(timeout=settings.admin_api_request_timeout_seconds) as client:
            response = await client.post(
                f"{settings.platform_api_url.rstrip('/')}/internal/admin-control/alert-lifecycle",
                json=payload,
                headers={"X-FlowWeave-Admin-Control-Key": settings.admin_control_api_key},
            )
    except httpx.HTTPError as exc:
        raise AdminControlError(
            status=503,
            code="ADMIN_CONTROL_UNAVAILABLE",
            message="The platform alert control service is unavailable",
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise AdminControlError(
            status=502,
            code="ADMIN_CONTROL_PROTOCOL_ERROR",
            message="The platform alert control service returned invalid data",
        ) from exc
    if response.is_error:
        error = body.get("error") if isinstance(body, dict) else None
        detail = error if isinstance(error, dict) else {}
        raise AdminControlError(
            status=response.status_code,
            code=str(detail.get("code") or "ADMIN_CONTROL_FAILED"),
            message=str(detail.get("message") or "The alert lifecycle update was rejected"),
        )
    if not isinstance(body, dict):
        raise AdminControlError(
            status=502,
            code="ADMIN_CONTROL_PROTOCOL_ERROR",
            message="The platform alert control service returned invalid data",
        )
    return body

    return body
