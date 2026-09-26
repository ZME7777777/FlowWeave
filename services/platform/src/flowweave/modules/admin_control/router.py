from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from flowweave.modules.agent_workspaces import public as agent_workspaces
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.sandboxes.infrastructure.models import FlowRunRuntime
from flowweave.modules.users.infrastructure.models import (
    AdminAlertAction,
    AdminAlertState,
    AdminRuntimeOperation,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, run_sync

router = APIRouter(prefix="/internal/admin-control")
AdminControlKey = Annotated[str | None, Header(alias="X-FlowWeave-Admin-Control-Key")]


class RuntimeReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_kind: Literal["FLOW_RUN", "AGENT_WORKSPACE"]
    owner_id: str = Field(min_length=36, max_length=36)
    flow_run_id: str | None = Field(default=None, min_length=36, max_length=36)
    runtime_session_id: str = Field(min_length=36, max_length=36)
    expected_generation: int = Field(ge=1)
    expected_session_row_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=200)
    actor_user_id: str = Field(min_length=36, max_length=36)
    actor_username: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=80)


class AlertLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_key: str = Field(min_length=3, max_length=300)
    action: Literal["ACKNOWLEDGE", "SILENCE"]
    reason: str = Field(min_length=10, max_length=500)
    silence_minutes: int | None = Field(default=None, ge=5, le=1_440)
    actor_user_id: str = Field(min_length=36, max_length=36)
    actor_username: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=80)


def require_admin_control_key(request: Request, key: AdminControlKey) -> None:
    settings = request.app.state.container.settings
    if (
        not settings.admin_control_api_key
        or key is None
        or not hmac.compare_digest(key, settings.admin_control_api_key)
    ):
        raise DomainError(
            "ADMIN_CONTROL_FORBIDDEN", "Administrator control authentication failed", 403
        )


@router.post("/runtime-replacements", status_code=202)
async def replace_runtime(
    payload: RuntimeReplacementRequest,
    db: Db,
    _: Annotated[None, Depends(require_admin_control_key)],
) -> dict[str, Any]:
    return await run_sync(db, lambda session: _request_replacement(session, payload))


@router.post("/alert-lifecycle")
async def update_alert_lifecycle(
    payload: AlertLifecycleRequest,
    db: Db,
    _: Annotated[None, Depends(require_admin_control_key)],
) -> dict[str, object]:
    return await run_sync(db, lambda session: _update_alert_lifecycle(session, payload))


def _update_alert_lifecycle(session: Any, payload: AlertLifecycleRequest) -> dict[str, object]:
    if payload.action == "ACKNOWLEDGE" and payload.silence_minutes is not None:
        raise DomainError(
            "ADMIN_ALERT_INVALID_ACTION", "Acknowledgement cannot set a silence duration", 422
        )
    if payload.action == "SILENCE" and payload.silence_minutes is None:
        raise DomainError("ADMIN_ALERT_INVALID_ACTION", "Silence duration is required", 422)
    now = datetime.now(UTC)
    state = session.get(AdminAlertState, payload.alert_key)
    if state is None:
        state = AdminAlertState(alert_key=payload.alert_key)
        session.add(state)
    silence_until = (
        now + timedelta(minutes=payload.silence_minutes)
        if payload.action == "SILENCE" and payload.silence_minutes is not None
        else state.silenced_until
    )
    if payload.action == "ACKNOWLEDGE":
        state.acknowledged_at = now
        state.acknowledged_by_user_id = payload.actor_user_id
        state.acknowledged_by_username = payload.actor_username
    else:
        state.silenced_until = silence_until
    state.reason = payload.reason
    session.add(
        AdminAlertAction(
            alert_key=payload.alert_key,
            action=payload.action,
            actor_user_id=payload.actor_user_id,
            actor_username=payload.actor_username,
            reason=payload.reason,
            silenced_until=silence_until if payload.action == "SILENCE" else None,
            request_id=payload.request_id,
        )
    )
    session.flush()
    return {
        "alert_key": state.alert_key,
        "acknowledged_at": state.acknowledged_at.isoformat() if state.acknowledged_at else None,
        "acknowledged_by_username": state.acknowledged_by_username,
        "silenced_until": state.silenced_until.isoformat() if state.silenced_until else None,
        "reason": state.reason,
    }


def _request_replacement(session: Any, payload: RuntimeReplacementRequest) -> dict[str, Any]:
    existing = session.scalar(
        select(AdminRuntimeOperation).where(
            AdminRuntimeOperation.actor_user_id == payload.actor_user_id,
            AdminRuntimeOperation.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.runtime_kind != payload.runtime_kind
            or existing.owner_id != payload.owner_id
            or existing.runtime_session_id != payload.runtime_session_id
            or existing.expected_generation != payload.expected_generation
            or existing.expected_session_row_version != payload.expected_session_row_version
            or existing.reason != payload.reason
        ):
            raise DomainError(
                "ADMIN_OPERATION_IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for a different Runtime replacement",
                409,
            )
        return _operation_response(existing, idempotent_replay=True)

    flow_run_id = payload.flow_run_id
    if payload.runtime_kind == "FLOW_RUN":
        if flow_run_id is None or payload.owner_id != flow_run_id:
            raise DomainError(
                "ADMIN_RUNTIME_REQUEST_INVALID",
                "A FlowRun Runtime replacement requires its matching FlowRun owner",
                422,
            )
        assert flow_run_id is not None
        runtime = session.scalar(
            select(FlowRunRuntime).where(
                FlowRunRuntime.id == payload.runtime_session_id,
                FlowRunRuntime.flow_run_id == flow_run_id,
                FlowRunRuntime.node_attempt_id.is_(None),
            )
        )
        if runtime is None:
            raise DomainError(
                "ADMIN_RUNTIME_NOT_FOUND", "The selected FlowRun Runtime was not found", 404
            )
    elif payload.flow_run_id is not None:
        raise DomainError(
            "ADMIN_RUNTIME_REQUEST_INVALID",
            "An Agent Workspace Runtime replacement cannot include a FlowRun owner",
            422,
        )

    operation = AdminRuntimeOperation(
        actor_user_id=payload.actor_user_id,
        actor_username=payload.actor_username,
        runtime_kind=payload.runtime_kind,
        owner_id=payload.owner_id,
        flow_run_id=flow_run_id,
        runtime_session_id=payload.runtime_session_id,
        expected_generation=payload.expected_generation,
        expected_session_row_version=payload.expected_session_row_version,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        request_id=payload.request_id,
    )
    session.add(operation)
    session.flush()
    if payload.runtime_kind == "FLOW_RUN":
        if flow_run_id is None:
            raise AssertionError("validated FlowRun replacement lost its owner")
        overview = sandboxes.request_runtime_replacement(
            session,
            flow_run_id,
            expected_generation=payload.expected_generation,
            expected_session_row_version=payload.expected_session_row_version,
        )
    else:
        overview = agent_workspaces.request_agent_workspace_runtime_replacement(
            session,
            workspace_id=payload.owner_id,
            runtime_session_id=payload.runtime_session_id,
            expected_generation=payload.expected_generation,
            expected_session_row_version=payload.expected_session_row_version,
        )
    return {
        "operation": _operation_response(operation, idempotent_replay=False),
        "runtime": overview,
    }


def _operation_response(
    operation: AdminRuntimeOperation, *, idempotent_replay: bool
) -> dict[str, object]:
    return {
        "id": operation.id,
        "action": operation.action,
        "status": operation.status,
        "runtime_kind": operation.runtime_kind,
        "owner_id": operation.owner_id,
        "flow_run_id": operation.flow_run_id,
        "runtime_session_id": operation.runtime_session_id,
        "expected_generation": operation.expected_generation,
        "expected_session_row_version": operation.expected_session_row_version,
        "reason": operation.reason,
        "created_at": operation.created_at.isoformat(),
        "idempotent_replay": idempotent_replay,
    }
