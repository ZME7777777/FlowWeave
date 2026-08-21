from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application import mcp_oauth_secrets
from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.modules.environments.public import lock_referenceable_version
from flowweave.modules.sandboxes.public import create_temporary_runtime, request_delete_durable
from flowweave.runtime.base import (
    RuntimeMCPOAuthCallbackRequest,
    RuntimeMCPOAuthJobRequest,
    RuntimeMCPOAuthStartRequest,
    RuntimeMCPOAuthStatus,
)
from flowweave.runtime.workspace import cleanup_mcp_probe, materialize_mcp_probe
from flowweave.shared.application.transactions import register_commit_action
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    MCPOAuthAuthorization,
    MCPOAuthSecretReference,
)
from flowweave.shared.schemas import (
    MCPOAuthAuthorizationCallbackWrite,
    MCPOAuthAuthorizationStartWrite,
)

_AUTHORIZATION_TTL = timedelta(minutes=15)
_ACTIVE_STATES = frozenset({"PENDING", "AUTHORIZING"})
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "EXPIRED"})


@dataclass(frozen=True, slots=True)
class AuthorizationStartPlan:
    authorization_id: str
    capability_key: str
    capability: dict[str, Any]
    environment_id: str
    environment_version_id: str
    environment_version_no: int
    image_digest: str
    timeout: float


@dataclass(frozen=True, slots=True)
class AuthorizationRuntimePlan:
    authorization_id: str
    expected_authorization_version: int
    request: RuntimeMCPOAuthJobRequest


def _authorization_dict(item: MCPOAuthAuthorization) -> dict[str, Any]:
    authorization_url: str | None = None
    if item.encrypted_authorization_url is not None and item.state in _ACTIVE_STATES:
        try:
            authorization_url = decrypt_secret(item.encrypted_authorization_url)
        except (InvalidToken, TypeError, UnicodeDecodeError) as exc:
            raise DomainError(
                "MCP_OAUTH_AUTHORIZATION_CORRUPT",
                "MCP OAuth authorization metadata cannot be decrypted",
                500,
            ) from exc
    return {
        "id": item.id,
        "secret_reference_id": item.secret_reference_id,
        "capability_version_id": item.capability_version_id,
        "environment_version_id": item.environment_version_id,
        "state": item.state,
        "state_version": item.state_version,
        "expected_secret_version": item.expected_secret_version,
        "persisted_secret_version": item.persisted_secret_version,
        "authorization_url": authorization_url,
        "callback_ready": item.callback_ready,
        "tool_catalog": list(item.tool_catalog_json or []),
        "error_code": item.error_code,
        "expires_at": item.expires_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _lock_reference(db: Session, reference_id: str) -> MCPOAuthSecretReference:
    item = db.scalar(
        select(MCPOAuthSecretReference)
        .where(MCPOAuthSecretReference.id == reference_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    return item


def _lock_authorization_with_reference(
    db: Session, authorization_id: str
) -> tuple[MCPOAuthSecretReference, MCPOAuthAuthorization]:
    reference_id = db.scalar(
        select(MCPOAuthAuthorization.secret_reference_id).where(
            MCPOAuthAuthorization.id == authorization_id
        )
    )
    if reference_id is None:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_NOT_FOUND",
            "MCP OAuth authorization was not found",
            404,
        )
    reference = _lock_reference(db, reference_id)
    authorization = db.scalar(
        select(MCPOAuthAuthorization)
        .where(MCPOAuthAuthorization.id == authorization_id)
        .with_for_update()
    )
    if authorization is None:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_NOT_FOUND",
            "MCP OAuth authorization was not found",
            404,
        )
    return reference, authorization


def _expire_if_needed(item: MCPOAuthAuthorization, now: datetime) -> bool:
    if item.state not in _ACTIVE_STATES or item.expires_at > now:
        return False
    item.state = "EXPIRED"
    item.state_version += 1
    item.error_code = "MCP_OAUTH_AUTHORIZATION_EXPIRED"
    item.encrypted_authorization_url = None
    item.completed_at = now
    item.updated_at = now
    return True


def _schedule_cleanup(db: Session, item: MCPOAuthAuthorization) -> None:
    """Reclaim an authorization Runtime only after its terminal state commits."""

    sandbox_id = item.sandbox_id
    authorization_id = item.id
    register_commit_action(
        db,
        lambda: request_delete_durable(db, sandbox_id),
    )
    register_commit_action(
        db,
        lambda: cleanup_mcp_probe(authorization_id),
    )


def begin_authorization(
    db: Session,
    secret_reference_id: str,
    payload: MCPOAuthAuthorizationStartWrite,
) -> AuthorizationStartPlan:
    reference = _lock_reference(db, secret_reference_id)
    if reference.state != "ACTIVE":
        raise DomainError(
            "MCP_OAUTH_SECRET_REVOKED",
            "MCP OAuth Secret Reference has been revoked",
            409,
        )
    if reference.state_version != payload.expected_state_version:
        raise DomainError(
            "MCP_OAUTH_SECRET_VERSION_CONFLICT",
            "MCP OAuth Secret Reference changed; refresh before authorizing",
            409,
            {"expected": payload.expected_state_version, "actual": reference.state_version},
        )
    if reference.encrypted_oauth_state is not None:
        raise DomainError(
            "MCP_OAUTH_ALREADY_AUTHORIZED",
            "Initial browser authorization requires an empty Secret Reference",
            409,
        )

    now = datetime.now(UTC)
    active = list(
        db.scalars(
            select(MCPOAuthAuthorization)
            .where(
                MCPOAuthAuthorization.secret_reference_id == reference.id,
                MCPOAuthAuthorization.state.in_(_ACTIVE_STATES),
            )
            .with_for_update()
        )
    )
    for previous in active:
        if _expire_if_needed(previous, now):
            _schedule_cleanup(db, previous)
    still_active = next((item for item in active if item.state in _ACTIVE_STATES), None)
    if still_active is not None:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_ACTIVE",
            "An MCP OAuth authorization is already active for this Secret Reference",
            409,
            {"authorization_id": still_active.id},
        )

    capability = resolve_version(db, reference.capability_version_id)
    if capability.package.capability_type != "MCP":
        raise DomainError(
            "MCP_CAPABILITY_REQUIRED",
            "MCP OAuth authorization requires an MCP Capability Version",
            422,
        )
    runtime_config = capability.runtime_config()
    mcp_oauth_secrets.require_oauth_capability(runtime_config)
    environment = lock_referenceable_version(db, reference.environment_version_id)
    if environment is None:
        raise DomainError(
            "ENVIRONMENT_VERSION_INVALID",
            "MCP OAuth authorization requires an active READY Environment Version",
            422,
        )

    authorization = MCPOAuthAuthorization(
        id=uid(),
        secret_reference_id=reference.id,
        capability_version_id=capability.version.id,
        environment_version_id=environment.id,
        state="PENDING",
        state_version=1,
        expected_secret_version=reference.state_version,
        callback_ready=False,
        tool_catalog_json=[],
        expires_at=now + _AUTHORIZATION_TTL,
    )
    db.add(authorization)
    db.flush()
    return AuthorizationStartPlan(
        authorization_id=authorization.id,
        capability_key=capability.package.capability_key,
        capability=runtime_config,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        image_digest=environment.image_digest,
        timeout=payload.timeout,
    )


def allocate_authorization_runtime(
    db: Session, plan: AuthorizationStartPlan
) -> RuntimeMCPOAuthStartRequest:
    _, item = _lock_authorization_with_reference(db, plan.authorization_id)
    if item.state != "PENDING" or item.state_version != 1:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_CONFLICT",
            "MCP OAuth authorization changed before Runtime allocation",
            409,
        )
    server, workspace_relative = materialize_mcp_probe(
        {
            "capability_key": plan.capability_key,
            "normalized_config": plan.capability,
        },
        plan.authorization_id,
    )
    allocation = create_temporary_runtime(
        db,
        owner_type="MCP_OAUTH_AUTHORIZATION",
        owner_id=plan.authorization_id,
        image=plan.image_digest,
        environment_id=plan.environment_id,
        environment_version_id=plan.environment_version_id,
        environment_version_no=plan.environment_version_no,
        workspace_relative=workspace_relative,
    )
    item.sandbox_id = allocation.id
    item.runtime_resource_name = allocation.resource_name
    item.runtime_base_url = allocation.base_url
    item.updated_at = datetime.now(UTC)
    db.flush()
    return RuntimeMCPOAuthStartRequest(
        server=server,
        base_url=allocation.base_url,
        runtime_resource_name=allocation.resource_name,
        timeout=plan.timeout,
    )


def _apply_status(
    db: Session,
    authorization_id: str,
    expected_authorization_version: int,
    status: RuntimeMCPOAuthStatus,
) -> dict[str, Any]:
    reference, item = _lock_authorization_with_reference(db, authorization_id)
    if item.state in _TERMINAL_STATES:
        return _authorization_dict(item)
    if item.state_version != expected_authorization_version:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_VERSION_CONFLICT",
            "MCP OAuth authorization changed; refresh before retrying",
            409,
            {"expected": expected_authorization_version, "actual": item.state_version},
        )
    if item.runtime_job_id is not None and status.job_id != item.runtime_job_id:
        raise DomainError(
            "RUNTIME_PROTOCOL_ERROR",
            "OpenHands returned a different MCP OAuth job identity",
            502,
        )
    now = datetime.now(UTC)
    if _expire_if_needed(item, now):
        db.flush()
        return _authorization_dict(item)
    if reference.state != "ACTIVE" or reference.state_version != item.expected_secret_version:
        item.state = "FAILED"
        item.state_version += 1
        item.error_code = (
            "MCP_OAUTH_SECRET_REVOKED"
            if reference.state != "ACTIVE"
            else "MCP_OAUTH_SECRET_VERSION_CONFLICT"
        )
        item.encrypted_authorization_url = None
        item.completed_at = now
        item.updated_at = now
        db.flush()
        return _authorization_dict(item)

    item.runtime_job_id = status.job_id
    item.callback_ready = status.callback_ready
    item.tool_catalog_json = list(status.tools)
    item.updated_at = now
    if status.authorization_url is not None:
        item.encrypted_authorization_url = encrypt_secret(status.authorization_url)

    if status.status == "succeeded":
        if status.oauth_state is None:
            raise DomainError(
                "RUNTIME_PROTOCOL_ERROR",
                "OpenHands completed MCP OAuth without returning OAuth state",
                502,
            )
        persisted_version = mcp_oauth_secrets.persist_authorized_state(
            db,
            secret_reference_id=reference.id,
            expected_state_version=item.expected_secret_version,
            authorization_id=item.id,
            state=status.oauth_state,
        )
        item.state = "SUCCEEDED"
        item.persisted_secret_version = persisted_version
        item.encrypted_authorization_url = None
        item.completed_at = now
    elif status.status == "failed" or not status.ok:
        item.state = "FAILED"
        item.error_code = f"MCP_OAUTH_RUNTIME_{(status.error_kind or 'unknown').upper()}"
        item.encrypted_authorization_url = None
        item.completed_at = now
    else:
        item.state = "AUTHORIZING" if status.status == "authorizing" else "PENDING"
    item.state_version += 1
    db.flush()
    return _authorization_dict(item)


def complete_start(
    db: Session, authorization_id: str, status: RuntimeMCPOAuthStatus
) -> dict[str, Any]:
    return _apply_status(db, authorization_id, 1, status)


def prepare_status(db: Session, authorization_id: str) -> AuthorizationRuntimePlan | dict[str, Any]:
    reference, item = _lock_authorization_with_reference(db, authorization_id)
    now = datetime.now(UTC)
    if _expire_if_needed(item, now):
        db.flush()
        return _authorization_dict(item)
    if item.state in _TERMINAL_STATES:
        return _authorization_dict(item)
    if reference.state != "ACTIVE" or reference.state_version != item.expected_secret_version:
        item.state = "FAILED"
        item.state_version += 1
        item.error_code = (
            "MCP_OAUTH_SECRET_REVOKED"
            if reference.state != "ACTIVE"
            else "MCP_OAUTH_SECRET_VERSION_CONFLICT"
        )
        item.encrypted_authorization_url = None
        item.completed_at = now
        item.updated_at = now
        db.flush()
        return _authorization_dict(item)
    if not item.runtime_job_id or not item.runtime_base_url or not item.runtime_resource_name:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_INCOMPLETE",
            "MCP OAuth authorization has no active Runtime job",
            409,
        )
    return AuthorizationRuntimePlan(
        authorization_id=item.id,
        expected_authorization_version=item.state_version,
        request=RuntimeMCPOAuthJobRequest(
            job_id=item.runtime_job_id,
            base_url=item.runtime_base_url,
            runtime_resource_name=item.runtime_resource_name,
        ),
    )


def complete_status(
    db: Session, plan: AuthorizationRuntimePlan, status: RuntimeMCPOAuthStatus
) -> dict[str, Any]:
    return _apply_status(db, plan.authorization_id, plan.expected_authorization_version, status)


def prepare_callback(
    db: Session,
    authorization_id: str,
    payload: MCPOAuthAuthorizationCallbackWrite,
) -> AuthorizationRuntimePlan:
    prepared = prepare_status(db, authorization_id)
    if isinstance(prepared, dict):
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_TERMINAL",
            "MCP OAuth authorization is no longer accepting callbacks",
            409,
            {"state": prepared["state"]},
        )
    if prepared.expected_authorization_version != payload.expected_authorization_version:
        raise DomainError(
            "MCP_OAUTH_AUTHORIZATION_VERSION_CONFLICT",
            "MCP OAuth authorization changed; refresh before submitting the callback",
            409,
            {
                "expected": payload.expected_authorization_version,
                "actual": prepared.expected_authorization_version,
            },
        )
    item = db.get(MCPOAuthAuthorization, authorization_id)
    if item is None or not item.callback_ready:
        raise DomainError(
            "MCP_OAUTH_CALLBACK_NOT_READY",
            "OpenHands OAuth callback listener is not ready",
            409,
        )
    return AuthorizationRuntimePlan(
        authorization_id=prepared.authorization_id,
        expected_authorization_version=prepared.expected_authorization_version,
        request=RuntimeMCPOAuthCallbackRequest(
            job_id=prepared.request.job_id,
            base_url=prepared.request.base_url,
            runtime_resource_name=prepared.request.runtime_resource_name,
            callback_url=payload.callback_url.get_secret_value(),
        ),
    )


def complete_callback(
    db: Session, plan: AuthorizationRuntimePlan, status: RuntimeMCPOAuthStatus
) -> dict[str, Any]:
    return complete_status(db, plan, status)


def read_authorization(db: Session, authorization_id: str) -> dict[str, Any]:
    _, item = _lock_authorization_with_reference(db, authorization_id)
    if _expire_if_needed(item, datetime.now(UTC)):
        _schedule_cleanup(db, item)
    db.flush()
    return _authorization_dict(item)


def fail_authorization(db: Session, authorization_id: str, error_code: str) -> dict[str, Any]:
    _, item = _lock_authorization_with_reference(db, authorization_id)
    if item.state in _ACTIVE_STATES:
        now = datetime.now(UTC)
        item.state = "FAILED"
        item.state_version += 1
        item.error_code = error_code[:100]
        item.encrypted_authorization_url = None
        item.completed_at = now
        item.updated_at = now
        db.flush()
    return _authorization_dict(item)


def cleanup_terminal(db: Session, authorization_id: str) -> None:
    item = db.get(MCPOAuthAuthorization, authorization_id)
    if item is None or item.state not in _TERMINAL_STATES:
        return
    _schedule_cleanup(db, item)


def authorization_owner_is_active(db: Session, authorization_id: str) -> bool:
    item = db.get(MCPOAuthAuthorization, authorization_id)
    return item is not None and item.state in _ACTIVE_STATES and item.expires_at > datetime.now(UTC)
