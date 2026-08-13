from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import UTC, datetime
from typing import Any, cast

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.modules.environments.public import lock_referenceable_version
from flowweave.modules.sandboxes.public import request_delete_durable
from flowweave.runtime.workspace import cleanup_mcp_probe
from flowweave.shared.application.transactions import register_commit_action
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    MCPOAuthAuthorization,
    MCPOAuthSecretAudit,
    MCPOAuthSecretReference,
)
from flowweave.shared.schemas import (
    MCPOAuthSecretReferenceRevokeWrite,
    MCPOAuthSecretReferenceWrite,
)

_MAX_OAUTH_STATE_BYTES = 256 * 1024
_OAUTH_STATE_FIELDS = frozenset({"tokens", "client_info", "token_expires_at"})


def _oauth_config(capability_config: dict[str, Any]) -> dict[str, Any] | None:
    auth = capability_config.get("auth")
    if not isinstance(auth, dict):
        return None
    value = cast(dict[str, Any], auth)
    return value if value.get("strategy") == "oauth2" else None


def require_oauth_capability(capability_config: dict[str, Any]) -> None:
    if _oauth_config(capability_config) is None:
        raise DomainError(
            "MCP_OAUTH_CAPABILITY_REQUIRED",
            "MCP OAuth Secret References require auth.strategy=oauth2",
            422,
        )


def _encoded_state(state: dict[str, Any]) -> bytes:
    if not state or set(state) - _OAUTH_STATE_FIELDS:
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned an invalid MCP OAuth state envelope",
            502,
        )
    if "tokens" in state and not isinstance(state["tokens"], dict):
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned an invalid MCP OAuth token state",
            502,
        )
    if "client_info" in state and not isinstance(state["client_info"], dict):
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned invalid MCP OAuth client state",
            502,
        )
    expiry = state.get("token_expires_at")
    if expiry is not None and (
        isinstance(expiry, bool)
        or not isinstance(expiry, int | float)
        or not math.isfinite(float(expiry))
    ):
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned invalid MCP OAuth expiry state",
            502,
        )
    try:
        encoded = json.dumps(
            state,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (RecursionError, TypeError, ValueError) as exc:
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned non-serializable MCP OAuth state",
            502,
        ) from exc
    if len(encoded) > _MAX_OAUTH_STATE_BYTES:
        raise DomainError(
            "MCP_OAUTH_STATE_INVALID",
            "OpenHands returned oversized MCP OAuth state",
            502,
        )
    return encoded


def _reference_dict(item: MCPOAuthSecretReference) -> dict[str, Any]:
    return {
        "id": item.id,
        "capability_version_id": item.capability_version_id,
        "environment_version_id": item.environment_version_id,
        "state": item.state,
        "state_version": item.state_version,
        "has_oauth_state": item.encrypted_oauth_state is not None,
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def create_reference(
    db: Session, capability_version_id: str, payload: MCPOAuthSecretReferenceWrite
) -> dict[str, Any]:
    capability = resolve_version(db, capability_version_id)
    if capability.package.capability_type != "MCP":
        raise DomainError(
            "MCP_CAPABILITY_REQUIRED",
            "OAuth Secret References require an MCP Capability Version",
            422,
        )
    require_oauth_capability(capability.runtime_config())
    environment = lock_referenceable_version(db, payload.environment_version_id)
    if environment is None:
        raise DomainError(
            "ENVIRONMENT_VERSION_INVALID",
            "MCP OAuth requires an active READY Environment Version",
            422,
            {"environment_version_id": payload.environment_version_id},
        )
    existing = db.scalar(
        select(MCPOAuthSecretReference).where(
            MCPOAuthSecretReference.capability_version_id == capability.version.id,
            MCPOAuthSecretReference.environment_version_id == environment.id,
        )
    )
    if existing is not None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_EXISTS",
            "An MCP OAuth Secret Reference already exists for this target",
            409,
            {"secret_reference_id": existing.id, "state": existing.state},
        )
    item = MCPOAuthSecretReference(
        id=uid(),
        capability_version_id=capability.version.id,
        environment_version_id=environment.id,
        state="ACTIVE",
        state_version=1,
    )
    db.add(item)
    db.flush()
    db.add(
        MCPOAuthSecretAudit(
            secret_reference_id=item.id,
            action="CREATED",
            state_version=item.state_version,
        )
    )
    db.flush()
    return _reference_dict(item)


def read_reference(db: Session, secret_reference_id: str) -> dict[str, Any]:
    item = db.get(MCPOAuthSecretReference, secret_reference_id)
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    result = _reference_dict(item)
    audits = list(
        db.scalars(
            select(MCPOAuthSecretAudit)
            .where(MCPOAuthSecretAudit.secret_reference_id == item.id)
            .order_by(MCPOAuthSecretAudit.created_at, MCPOAuthSecretAudit.id)
        )
    )
    result["audit"] = [
        {
            "id": audit.id,
            "validation_id": audit.validation_id,
            "action": audit.action,
            "state_version": audit.state_version,
            "created_at": audit.created_at.isoformat(),
        }
        for audit in audits
    ]
    return result


def load_state(
    db: Session,
    *,
    secret_reference_id: str,
    expected_state_version: int,
    capability_version_id: str,
    environment_version_id: str,
) -> dict[str, Any] | None:
    item = db.scalar(
        select(MCPOAuthSecretReference)
        .where(MCPOAuthSecretReference.id == secret_reference_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    if (
        item.capability_version_id != capability_version_id
        or item.environment_version_id != environment_version_id
    ):
        raise DomainError(
            "MCP_OAUTH_SECRET_TARGET_MISMATCH",
            "MCP OAuth Secret Reference does not match the validation target",
            422,
        )
    if item.state != "ACTIVE":
        raise DomainError(
            "MCP_OAUTH_SECRET_REVOKED",
            "MCP OAuth Secret Reference has been revoked",
            409,
        )
    if item.state_version != expected_state_version:
        raise DomainError(
            "MCP_OAUTH_SECRET_VERSION_CONFLICT",
            "MCP OAuth Secret Reference changed; refresh before retrying",
            409,
            {"expected": expected_state_version, "actual": item.state_version},
        )
    if item.encrypted_oauth_state is None:
        return None
    try:
        value = json.loads(decrypt_secret(item.encrypted_oauth_state))
    except (InvalidToken, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise DomainError(
            "MCP_OAUTH_SECRET_CORRUPT",
            "MCP OAuth Secret Reference cannot be decrypted",
            500,
        ) from exc
    if not isinstance(value, dict):
        raise DomainError(
            "MCP_OAUTH_SECRET_CORRUPT",
            "MCP OAuth Secret Reference contains invalid state",
            500,
        )
    state = cast(dict[str, Any], value)
    try:
        encoded = _encoded_state(state)
    except DomainError as exc:
        raise DomainError(
            "MCP_OAUTH_SECRET_CORRUPT",
            "MCP OAuth Secret Reference contains invalid state",
            500,
        ) from exc
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if item.oauth_state_digest is None or not hmac.compare_digest(
        item.oauth_state_digest, actual_digest
    ):
        raise DomainError(
            "MCP_OAUTH_SECRET_CORRUPT",
            "MCP OAuth Secret Reference failed integrity verification",
            500,
        )
    return state


def persist_refreshed_state(
    db: Session,
    *,
    secret_reference_id: str,
    expected_state_version: int,
    validation_id: str | None,
    state: dict[str, Any],
) -> int:
    encoded = _encoded_state(state)
    item = db.scalar(
        select(MCPOAuthSecretReference)
        .where(MCPOAuthSecretReference.id == secret_reference_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    if item.state != "ACTIVE":
        raise DomainError(
            "MCP_OAUTH_SECRET_REVOKED",
            "MCP OAuth Secret Reference was revoked while validation was running",
            409,
        )
    if item.state_version != expected_state_version:
        raise DomainError(
            "MCP_OAUTH_SECRET_VERSION_CONFLICT",
            "MCP OAuth Secret Reference changed while validation was running",
            409,
            {"expected": expected_state_version, "actual": item.state_version},
        )
    digest = hashlib.sha256(encoded).hexdigest()
    item.encrypted_oauth_state = encrypt_secret(encoded.decode())
    item.oauth_state_digest = digest
    item.state_version += 1
    item.updated_at = datetime.now(UTC)
    db.add(
        MCPOAuthSecretAudit(
            secret_reference_id=item.id,
            validation_id=validation_id,
            action="REFRESHED",
            state_version=item.state_version,
            oauth_state_digest=digest,
        )
    )
    db.flush()
    return item.state_version


def persist_authorized_state(
    db: Session,
    *,
    secret_reference_id: str,
    expected_state_version: int,
    authorization_id: str,
    state: dict[str, Any],
) -> int:
    """Persist first authorization state with the same Secret CAS boundary."""

    encoded = _encoded_state(state)
    item = db.scalar(
        select(MCPOAuthSecretReference)
        .where(MCPOAuthSecretReference.id == secret_reference_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    if item.state != "ACTIVE":
        raise DomainError(
            "MCP_OAUTH_SECRET_REVOKED",
            "MCP OAuth Secret Reference was revoked while authorization was running",
            409,
        )
    if item.state_version != expected_state_version:
        raise DomainError(
            "MCP_OAUTH_SECRET_VERSION_CONFLICT",
            "MCP OAuth Secret Reference changed while authorization was running",
            409,
            {"expected": expected_state_version, "actual": item.state_version},
        )
    if item.encrypted_oauth_state is not None:
        raise DomainError(
            "MCP_OAUTH_ALREADY_AUTHORIZED",
            "Initial authorization cannot replace existing OAuth state",
            409,
        )
    digest = hashlib.sha256(encoded).hexdigest()
    item.encrypted_oauth_state = encrypt_secret(encoded.decode())
    item.oauth_state_digest = digest
    item.state_version += 1
    item.updated_at = datetime.now(UTC)
    db.add(
        MCPOAuthSecretAudit(
            secret_reference_id=item.id,
            authorization_id=authorization_id,
            action="AUTHORIZED",
            state_version=item.state_version,
            oauth_state_digest=digest,
        )
    )
    db.flush()
    return item.state_version


def revoke_reference(
    db: Session, secret_reference_id: str, payload: MCPOAuthSecretReferenceRevokeWrite
) -> dict[str, Any]:
    item = db.scalar(
        select(MCPOAuthSecretReference)
        .where(MCPOAuthSecretReference.id == secret_reference_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_NOT_FOUND",
            "MCP OAuth Secret Reference was not found",
            404,
        )
    if item.state == "REVOKED":
        return _reference_dict(item)
    if item.state_version != payload.expected_state_version:
        raise DomainError(
            "MCP_OAUTH_SECRET_VERSION_CONFLICT",
            "MCP OAuth Secret Reference changed; refresh before revoking",
            409,
            {"expected": payload.expected_state_version, "actual": item.state_version},
        )
    item.state = "REVOKED"
    item.state_version += 1
    item.encrypted_oauth_state = None
    item.oauth_state_digest = None
    revoked_at = datetime.now(UTC)
    item.revoked_at = revoked_at
    item.updated_at = revoked_at
    active_authorizations = list(
        db.scalars(
            select(MCPOAuthAuthorization)
            .where(
                MCPOAuthAuthorization.secret_reference_id == item.id,
                MCPOAuthAuthorization.state.in_(("PENDING", "AUTHORIZING")),
            )
            .with_for_update()
        )
    )
    for authorization in active_authorizations:
        authorization.state = "FAILED"
        authorization.state_version += 1
        authorization.error_code = "MCP_OAUTH_SECRET_REVOKED"
        authorization.encrypted_authorization_url = None
        authorization.completed_at = revoked_at
        authorization.updated_at = revoked_at
        sandbox_id = authorization.sandbox_id
        authorization_id = authorization.id
        register_commit_action(
            db,
            lambda sandbox_id=sandbox_id: request_delete_durable(db, sandbox_id),
        )
        register_commit_action(
            db,
            lambda authorization_id=authorization_id: cleanup_mcp_probe(authorization_id),
        )
    db.add(
        MCPOAuthSecretAudit(
            secret_reference_id=item.id,
            action="REVOKED",
            state_version=item.state_version,
        )
    )
    db.flush()
    return _reference_dict(item)
