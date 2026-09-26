from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.agent_workspaces import public as agent_workspaces
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    ManagedSandbox,
    RuntimeGeneration,
)
from flowweave.modules.users.infrastructure.models import (
    AdminAlertAction,
    AdminAlertState,
    AdminRuntimeOperation,
    RuntimeBusinessObservation,
)
from flowweave.runtime.base import RuntimeEventBatch, RuntimeInputReadiness
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, run_sync

router = APIRouter(prefix="/internal/admin-control")
AdminControlKey = Annotated[str | None, Header(alias="X-FlowWeave-Admin-Control-Key")]


class RuntimeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["REPLACE_RUNTIME", "ISOLATE_RUNTIME", "RESUME_RUNTIME"]
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


class RuntimeDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_kind: Literal["FLOW_RUN", "AGENT_WORKSPACE"]
    owner_id: str = Field(min_length=36, max_length=36)
    runtime_session_id: str = Field(min_length=36, max_length=36)
    expected_generation: int = Field(ge=1)
    expected_session_row_version: int = Field(ge=1)
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


@router.post("/runtime-controls", status_code=202)
async def control_runtime(
    payload: RuntimeControlRequest,
    db: Db,
    _: Annotated[None, Depends(require_admin_control_key)],
) -> dict[str, Any]:
    return await run_sync(db, lambda session: _control_runtime(session, payload))


@router.post("/runtime-diagnostics")
async def diagnose_runtime(
    payload: RuntimeDiagnosticRequest,
    db: Db,
    _: Annotated[None, Depends(require_admin_control_key)],
) -> dict[str, object]:
    return await run_sync(db, lambda session: _diagnose_runtime(session, payload))


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


def _control_runtime(session: Any, payload: RuntimeControlRequest) -> dict[str, Any]:
    existing = session.scalar(
        select(AdminRuntimeOperation).where(
            AdminRuntimeOperation.actor_user_id == payload.actor_user_id,
            AdminRuntimeOperation.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.action != payload.action
            or existing.runtime_kind != payload.runtime_kind
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
        action=payload.action,
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
    if payload.action == "REPLACE_RUNTIME":
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
    else:
        overview = _set_runtime_isolation(session, payload)

    return {
        "operation": _operation_response(operation, idempotent_replay=False),
        "runtime": overview,
    }


def _runtime_for_control(session: Any, payload: RuntimeControlRequest) -> Any:
    model = FlowRunRuntime if payload.runtime_kind == "FLOW_RUN" else AgentWorkspaceRuntime
    owner_field = (
        FlowRunRuntime.flow_run_id
        if payload.runtime_kind == "FLOW_RUN"
        else AgentWorkspaceRuntime.workspace_id
    )
    runtime = session.scalar(
        select(model)
        .where(model.id == payload.runtime_session_id, owner_field == payload.owner_id)
        .with_for_update()
    )
    if runtime is None:
        raise DomainError("ADMIN_RUNTIME_NOT_FOUND", "The selected Runtime was not found", 404)
    if (
        runtime.active_generation != payload.expected_generation
        or runtime.row_version != payload.expected_session_row_version
    ):
        raise DomainError(
            "ADMIN_RUNTIME_VERSION_CONFLICT", "The Runtime changed; refresh before retrying", 409
        )
    return runtime


def _set_runtime_isolation(session: Any, payload: RuntimeControlRequest) -> dict[str, object]:
    runtime = _runtime_for_control(session, payload)
    if payload.action == "ISOLATE_RUNTIME":
        if runtime.status not in {"ACTIVE", "DEGRADED"}:
            raise DomainError(
                "RUNTIME_ISOLATION_NOT_ALLOWED",
                "The Runtime cannot be isolated in its current state",
                409,
            )
        runtime.status = "MAINTENANCE"
        if payload.runtime_kind == "AGENT_WORKSPACE":
            runtime.failure_code = "ADMIN_RUNTIME_ISOLATED"
            runtime.failure_summary = "An administrator isolated new Runtime writes"
    elif payload.action == "RESUME_RUNTIME":
        if runtime.status != "MAINTENANCE":
            raise DomainError("RUNTIME_RESUME_NOT_ALLOWED", "The Runtime is not isolated", 409)
        resource = _current_managed_runtime(session, runtime, payload.runtime_kind)
        if resource is not None and (
            resource.desired_state != "RUNNING" or resource.observed_state != "RUNNING"
        ):
            resource = None
        if resource is None:
            raise DomainError(
                "RUNTIME_RESUME_UNAVAILABLE",
                "The active Runtime resource is not ready to resume",
                409,
            )
        runtime.status = "ACTIVE"
        if payload.runtime_kind == "AGENT_WORKSPACE":
            runtime.failure_code = None
            runtime.failure_summary = None
    else:
        raise DomainError("ADMIN_RUNTIME_ACTION_INVALID", "Unsupported Runtime control action", 422)
    runtime.row_version += 1
    runtime.updated_at = datetime.now(UTC)
    session.flush()
    return {
        "runtime_session_id": runtime.id,
        "runtime_kind": payload.runtime_kind,
        "status": runtime.status,
    }


def _current_managed_runtime(
    session: Any, runtime: Any, runtime_kind: str
) -> ManagedSandbox | None:
    if runtime_kind == "FLOW_RUN":
        return session.scalar(
            select(ManagedSandbox)
            .join(RuntimeGeneration, RuntimeGeneration.managed_runtime_id == ManagedSandbox.id)
            .where(
                RuntimeGeneration.runtime_session_id == runtime.id,
                RuntimeGeneration.generation == runtime.active_generation,
            )
        )
    return session.scalar(
        select(ManagedSandbox)
        .join(
            AgentWorkspaceRuntimeGeneration,
            AgentWorkspaceRuntimeGeneration.managed_runtime_id == ManagedSandbox.id,
        )
        .where(
            AgentWorkspaceRuntimeGeneration.runtime_session_id == runtime.id,
            AgentWorkspaceRuntimeGeneration.generation == runtime.active_generation,
        )
    )


def _diagnose_runtime(session: Any, payload: RuntimeDiagnosticRequest) -> dict[str, object]:
    model = FlowRunRuntime if payload.runtime_kind == "FLOW_RUN" else AgentWorkspaceRuntime
    owner_field = (
        FlowRunRuntime.flow_run_id
        if payload.runtime_kind == "FLOW_RUN"
        else AgentWorkspaceRuntime.workspace_id
    )
    runtime = session.scalar(
        select(model).where(model.id == payload.runtime_session_id, owner_field == payload.owner_id)
    )
    if runtime is None:
        raise DomainError("ADMIN_RUNTIME_NOT_FOUND", "The selected Runtime was not found", 404)
    if (
        runtime.active_generation != payload.expected_generation
        or runtime.row_version != payload.expected_session_row_version
    ):
        raise DomainError(
            "ADMIN_RUNTIME_VERSION_CONFLICT", "The Runtime changed; refresh before diagnosing", 409
        )
    if runtime.status != "ACTIVE":
        raise DomainError(
            "RUNTIME_DIAGNOSTIC_NOT_ALLOWED",
            "The Runtime must be active before running a formal business diagnostic",
            409,
        )
    binding = session.scalar(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.runtime_session_id == runtime.id,
            AgentConversationBinding.lifecycle == "ACTIVE",
        )
        .order_by(
            AgentConversationBinding.last_connected_at.desc().nullslast(),
            AgentConversationBinding.updated_at.desc(),
        )
        .limit(1)
    )
    if binding is None:
        observation = RuntimeBusinessObservation(
            runtime_kind=payload.runtime_kind,
            runtime_session_id=runtime.id,
            generation=payload.expected_generation,
            source="MANUAL",
            status="NO_ACTIVE_CONVERSATION",
            impacted_bindings=0,
            stages_json=[],
        )
        session.add(observation)
        session.flush()
        return {
            "status": "NO_ACTIVE_CONVERSATION",
            "stages": [],
            "impacted_bindings": 0,
            "event_count": None,
            "readiness": None,
            "runtime_availability": None,
        }
    if binding.host_kind == "AGENT_WORKSPACE":
        from flowweave.modules.agent_sessions.application import conversations

        handle = conversations.runtime_stream_details(
            session, str(binding.workspace_id), binding.id
        )[1]
    else:
        from flowweave.modules.agent_sessions.application import flow_node_conversations

        handle = flow_node_conversations.runtime_stream_details(session, binding.id)[1]
    native = get_runtime()
    stages: list[dict[str, object]] = []

    def probe(name: str, operation: Any) -> object | None:
        started = monotonic()
        try:
            value = operation()
            stages.append(
                {"name": name, "outcome": "ok", "duration_ms": int((monotonic() - started) * 1000)}
            )
            return value
        except DomainError as exc:
            stages.append(
                {
                    "name": name,
                    "outcome": "error",
                    "duration_ms": int((monotonic() - started) * 1000),
                    "error_code": exc.code,
                }
            )
        except Exception:
            stages.append(
                {
                    "name": name,
                    "outcome": "error",
                    "duration_ms": int((monotonic() - started) * 1000),
                    "error_code": "RUNTIME_DIAGNOSTIC_UNCLASSIFIED",
                }
            )
        return None

    availability = probe("conversation_runtime", lambda: native.conversation_runtime(handle))
    batch_value = probe("active_event_window", lambda: native.read_active_events(handle))
    readiness_value = probe("input_readiness", lambda: native.input_readiness(handle))
    batch = batch_value if isinstance(batch_value, RuntimeEventBatch) else None
    readiness = readiness_value if isinstance(readiness_value, RuntimeInputReadiness) else None
    impacted_bindings = int(
        session.scalar(
            select(func.count(AgentConversationBinding.id)).where(
                AgentConversationBinding.runtime_session_id == runtime.id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
        or 0
    )
    status = "OK" if all(item["outcome"] == "ok" for item in stages) else "DEGRADED"
    readiness_dict = readiness.as_dict() if readiness is not None else None
    availability_status = getattr(availability, "status", None)
    session.add(
        RuntimeBusinessObservation(
            runtime_kind=payload.runtime_kind,
            runtime_session_id=runtime.id,
            generation=payload.expected_generation,
            source="MANUAL",
            status=status,
            representative_binding_id=binding.id,
            impacted_bindings=impacted_bindings,
            event_count=len(batch.events) if batch is not None else None,
            readiness_status=(
                str(readiness_dict.get("execution_status")) if readiness_dict is not None else None
            ),
            runtime_availability=str(availability_status) if availability_status else None,
            stages_json=stages,
        )
    )
    session.flush()
    return {
        "status": status,
        "representative_binding_id": binding.id,
        "impacted_bindings": impacted_bindings,
        "stages": stages,
        "event_count": len(batch.events) if batch is not None else None,
        "readiness": readiness_dict,
        "runtime_availability": availability_status,
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
