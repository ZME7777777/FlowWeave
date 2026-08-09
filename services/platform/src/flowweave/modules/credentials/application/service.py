from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CredentialConnection, CredentialLease, OAuthSession
from flowweave.shared.settings import get_settings


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def oauth_exchange_failed() -> DomainError:
    return DomainError(
        "OAUTH_EXCHANGE_FAILED",
        "Lark rejected the OAuth code exchange",
        502,
    )


def invalid_internal_credential() -> DomainError:
    return DomainError(
        "CREDENTIAL_LOOKUP_UNAUTHORIZED",
        "Credential lookup is not authorized",
        401,
    )


def start_lark_oauth(db: Session, subject_key: str, scopes: list[str]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.lark_oauth_client_id or not settings.lark_oauth_client_secret:
        raise DomainError(
            "OAUTH_PROVIDER_NOT_CONFIGURED",
            "Lark OAuth is not configured",
            503,
        )
    state = token_urlsafe(32)
    verifier = token_urlsafe(64)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.oauth_session_ttl_seconds)
    db.add(
        OAuthSession(
            provider="lark",
            subject_key=subject_key,
            state_digest=_digest(state),
            encrypted_code_verifier=encrypt_secret(verifier),
            scopes_json=scopes,
            expires_at=expires_at,
        )
    )
    db.commit()
    query = urlencode(
        {
            "client_id": settings.lark_oauth_client_id,
            "redirect_uri": settings.lark_oauth_redirect_url,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return {
        "authorization_url": f"{settings.lark_oauth_authorize_url}?{query}",
        "expires_at": expires_at.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class OAuthExchange:
    subject_key: str
    verifier: str
    scopes: tuple[str, ...]


def consume_oauth_state(db: Session, state: str) -> OAuthExchange:
    now = datetime.now(UTC)
    digest = _digest(state)
    session_id = db.scalar(
        update(OAuthSession)
        .where(
            OAuthSession.state_digest == digest,
            OAuthSession.provider == "lark",
            OAuthSession.consumed_at.is_(None),
            OAuthSession.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(OAuthSession.id)
        .execution_options(synchronize_session=False)
    )
    if session_id is None:
        db.rollback()
        raise DomainError("OAUTH_STATE_INVALID", "OAuth state is invalid or expired", 400)
    item = db.get(OAuthSession, session_id)
    if item is None:
        db.rollback()
        raise DomainError("OAUTH_STATE_INVALID", "OAuth state is invalid or expired", 400)
    result = OAuthExchange(
        subject_key=item.subject_key,
        verifier=decrypt_secret(item.encrypted_code_verifier),
        scopes=tuple(item.scopes_json or []),
    )
    db.commit()
    return result


def save_lark_connection(
    db: Session,
    exchange: OAuthExchange,
    token_response: dict[str, Any],
) -> dict[str, Any]:
    access_token = str(token_response.get("access_token") or "")
    if not access_token:
        raise DomainError("OAUTH_EXCHANGE_FAILED", "OAuth response has no access token", 502)
    refresh_token_value = token_response.get("refresh_token")
    refresh_token = str(refresh_token_value) if refresh_token_value else None
    expires_in = int(token_response.get("expires_in") or 3600)
    provider_subject_value = token_response.get("open_id") or token_response.get("user_id")
    provider_subject = str(provider_subject_value) if provider_subject_value else None
    scopes_value = token_response.get("scope")
    scopes = str(scopes_value).split() if scopes_value else list(exchange.scopes)
    item = db.scalar(
        select(CredentialConnection).where(
            CredentialConnection.provider == "lark",
            CredentialConnection.subject_key == exchange.subject_key,
        )
    )
    if item is None:
        item = CredentialConnection(
            provider="lark", subject_key=exchange.subject_key, row_version=1
        )
        db.add(item)
    else:
        item.row_version += 1
    item.provider_subject = provider_subject
    item.encrypted_access_token = encrypt_secret(access_token)
    if refresh_token is not None:
        item.encrypted_refresh_token = encrypt_secret(refresh_token)
    item.scopes_json = scopes
    item.access_expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 60))
    item.state = "CONNECTED"
    item.revoked_at = None
    db.commit()
    return connection_dict(item)


def connection_dict(item: CredentialConnection) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.provider,
        "provider_subject": item.provider_subject,
        "scopes": list(item.scopes_json or []),
        "state": item.state,
        "expires_at": item.access_expires_at.isoformat() if item.access_expires_at else None,
        "updated_at": item.updated_at.isoformat(),
    }


def list_connections(db: Session, subject_key: str) -> list[dict[str, Any]]:
    return [
        connection_dict(item)
        for item in db.scalars(
            select(CredentialConnection)
            .where(
                CredentialConnection.subject_key == subject_key,
                CredentialConnection.revoked_at.is_(None),
            )
            .order_by(CredentialConnection.provider)
        )
    ]


def connected_lark_access_token(db: Session) -> str:
    """Return the platform user's Lark token for server-side Drive orchestration."""

    settings = get_settings()
    if settings.runtime_adapter == "mock":
        return "mock-lark-access-token"
    item = db.scalar(
        select(CredentialConnection).where(
            CredentialConnection.subject_key == settings.credential_subject_key,
            CredentialConnection.provider == "lark",
            CredentialConnection.state == "CONNECTED",
            CredentialConnection.revoked_at.is_(None),
        )
    )
    if item is None:
        raise DomainError(
            "CREDENTIAL_CONNECTION_REQUIRED",
            "A connected Lark account is required to create run documents",
            409,
        )
    if item.access_expires_at is not None and _utc(item.access_expires_at) <= datetime.now(UTC):
        raise DomainError(
            "CREDENTIAL_REAUTH_REQUIRED",
            "The Lark connection must be renewed before creating run documents",
            409,
        )
    return decrypt_secret(item.encrypted_access_token)


def revoke_connection(db: Session, subject_key: str, connection_id: str) -> None:
    item = db.get(CredentialConnection, connection_id)
    if item is None or item.subject_key != subject_key:
        raise DomainError("RESOURCE_NOT_FOUND", "Credential connection not found", 404)
    item.revoked_at = datetime.now(UTC)
    item.state = "REVOKED"
    db.execute(
        update(CredentialLease)
        .where(CredentialLease.connection_id == item.id, CredentialLease.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()


def issue_runtime_lease(
    db: Session,
    *,
    subject_key: str,
    provider: str,
    audience: str,
    scopes: list[str],
) -> str:
    now = datetime.now(UTC)
    connection = db.scalar(
        select(CredentialConnection).where(
            CredentialConnection.subject_key == subject_key,
            CredentialConnection.provider == provider,
            CredentialConnection.state == "CONNECTED",
            CredentialConnection.revoked_at.is_(None),
        )
    )
    if connection is None:
        raise DomainError(
            "CREDENTIAL_CONNECTION_REQUIRED",
            f"A connected {provider} account is required",
            409,
        )
    granted = set(connection.scopes_json or [])
    if not set(scopes).issubset(granted):
        raise DomainError(
            "CREDENTIAL_SCOPE_DENIED",
            "Requested credential scope is unavailable",
            403,
        )
    if connection.access_expires_at is not None and _utc(connection.access_expires_at) <= now:
        raise DomainError(
            "CREDENTIAL_REAUTH_REQUIRED",
            "Credential connection must be renewed",
            409,
        )
    token = token_urlsafe(40)
    ttl = get_settings().credential_lease_ttl_seconds
    db.add(
        CredentialLease(
            connection_id=connection.id,
            token_digest=_digest(token),
            audience=audience,
            scopes_json=scopes,
            expires_at=now + timedelta(seconds=ttl),
            max_uses=get_settings().credential_lease_max_uses,
        )
    )
    db.commit()
    return token


def issue_optional_runtime_lookup(
    db: Session, *, audience: str
) -> tuple[str, tuple[str, ...]] | None:
    """Create a short-lived opaque lookup URL for a connected Lark account."""

    settings = get_settings()
    now = datetime.now(UTC)
    connection = db.scalar(
        select(CredentialConnection).where(
            CredentialConnection.subject_key == settings.credential_subject_key,
            CredentialConnection.provider == "lark",
            CredentialConnection.state == "CONNECTED",
            CredentialConnection.revoked_at.is_(None),
        )
    )
    if (
        connection is None
        or connection.access_expires_at is None
        or _utc(connection.access_expires_at) <= now
    ):
        return None
    scopes = tuple(connection.scopes_json or [])
    token = issue_runtime_lease(
        db,
        subject_key=settings.credential_subject_key,
        provider="lark",
        audience=audience,
        scopes=list(scopes),
    )
    url = f"{settings.credential_internal_base_url.rstrip('/')}/internal/credential-leases/{token}"
    return url, scopes


def consume_runtime_lease(db: Session, token: str) -> str:
    now = datetime.now(UTC)
    lease_id = db.scalar(
        update(CredentialLease)
        .where(
            CredentialLease.token_digest == _digest(token),
            CredentialLease.revoked_at.is_(None),
            CredentialLease.expires_at > now,
            CredentialLease.use_count < CredentialLease.max_uses,
        )
        .values(use_count=CredentialLease.use_count + 1, last_used_at=now)
        .returning(CredentialLease.id)
        .execution_options(synchronize_session=False)
    )
    if lease_id is None:
        db.rollback()
        raise DomainError("CREDENTIAL_LEASE_INVALID", "Credential lease is invalid or expired", 401)
    lease = db.get(CredentialLease, lease_id)
    connection = db.get(CredentialConnection, lease.connection_id) if lease else None
    if connection is None or connection.revoked_at is not None or connection.state != "CONNECTED":
        db.rollback()
        raise DomainError("CREDENTIAL_LEASE_INVALID", "Credential lease is invalid or expired", 401)
    value = decrypt_secret(connection.encrypted_access_token)
    db.commit()
    return value
