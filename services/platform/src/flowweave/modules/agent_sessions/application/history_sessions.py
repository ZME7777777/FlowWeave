"""Stable history-only credentials with renewable, explicitly authorized source leases."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations, history_access
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentHistorySession,
    AgentHistorySource,
)
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError


@dataclass(frozen=True)
class AuthorizedSource:
    id: str
    owner_user_id: str
    workspace_id: str
    source_binding_id: str


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _assert_binding(
    binding: AgentConversationBinding,
    owner: str,
    workspace_id: str,
    native_id: str | None = None,
    runtime_id: str | None = None,
    *,
    allow_provisioning: bool = False,
) -> None:
    if (
        binding.owner_user_id != owner
        or binding.host_kind != "AGENT_WORKSPACE"
        or binding.host_id != workspace_id
        or binding.lifecycle
        not in ({"ACTIVE", "PROVISIONING"} if allow_provisioning else {"ACTIVE"})
        or native_id is not None
        and binding.openhands_conversation_id != native_id
        or runtime_id is not None
        and binding.runtime_session_id != runtime_id
    ):
        raise history_access.denied()


def _target(
    db: Session, workspace_id: str, target_id: str, *, allow_provisioning: bool = False
) -> AgentConversationBinding:
    target, _ = conversations.history_reference_bindings(
        db, workspace_id, target_id, target_id, lock_target=True
    )
    owner = current_user_id()
    if agent_session_principal(db, owner) is None:
        raise history_access.denied()
    _assert_binding(target, owner, workspace_id, allow_provisioning=allow_provisioning)
    return target


def _target_session(db: Session, target_id: str) -> AgentHistorySession | None:
    return db.scalar(
        select(AgentHistorySession).where(
            AgentHistorySession.target_binding_id == target_id,
            AgentHistorySession.owner_user_id == current_user_id(),
        )
    )


def _source(db: Session, session_id: str, source_id: str) -> AgentHistorySource | None:
    return db.scalar(
        select(AgentHistorySource).where(
            AgentHistorySource.session_id == session_id,
            AgentHistorySource.source_binding_id == source_id,
            AgentHistorySource.owner_user_id == current_user_id(),
        )
    )


def renew(
    db: Session,
    workspace_id: str,
    target_id: str,
    source_ids: tuple[str, ...],
    ttl_seconds: int = 900,
    *,
    allow_provisioning: bool = False,
) -> tuple[AgentHistorySession, str]:
    """Only an authenticated control-plane action renews leases, never an MCP read.

    The locked target serializes concurrent issuance and revocation. Expired
    leases can be renewed; revoked sessions or sources stay revoked.
    """

    if (
        not 1 <= len(source_ids) <= 10
        or len(set(source_ids)) != len(source_ids)
        or any(len(value) != 36 or value != value.strip() for value in source_ids)
    ):
        raise DomainError("AGENT_HISTORY_SOURCES_INVALID", "请选择 1 到 10 个不同的来源会话", 422)
    if isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 3600:
        raise DomainError("AGENT_HISTORY_TTL_INVALID", "授权时长必须在 60 到 3600 秒之间", 422)
    target = _target(db, workspace_id, target_id, allow_provisioning=allow_provisioning)
    owner = current_user_id()
    lease = _target_session(db, target.id)
    if lease is not None:
        if lease.revoked_at is not None or lease.workspace_id != workspace_id:
            raise history_access.denied()
        _assert_binding(
            target,
            owner,
            workspace_id,
            lease.target_conversation_id,
            lease.target_runtime_session_id,
            allow_provisioning=allow_provisioning,
        )
    sources: list[tuple[AgentConversationBinding, AgentHistorySource | None]] = []
    for source_id in source_ids:
        _, binding = conversations.history_reference_bindings(
            db, workspace_id, target.id, source_id
        )
        previous = _source(db, lease.id, source_id) if lease is not None else None
        _assert_binding(binding, owner, workspace_id)
        if previous is not None:
            if previous.revoked_at is not None:
                raise history_access.denied()
            _assert_binding(
                binding,
                owner,
                workspace_id,
                previous.source_conversation_id,
                previous.source_runtime_session_id,
            )
        sources.append((binding, previous))
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    if lease is None:
        identity = uid()
        token = f"fwh2.{owner}.{identity}.{secrets.token_urlsafe(32)}"
        lease = AgentHistorySession(
            id=identity,
            workspace_id=workspace_id,
            target_binding_id=target.id,
            target_conversation_id=target.openhands_conversation_id,
            target_runtime_session_id=target.runtime_session_id,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            encrypted_token=encrypt_secret(token),
            expires_at=expires_at,
        )
        db.add(lease)
    else:
        try:
            token = decrypt_secret(lease.encrypted_token)
            identity = history_access.token_identity(token)
        except Exception:
            raise history_access.denied() from None
        if (
            not token.startswith("fwh2.")
            or identity != (owner, lease.id)
            or not hmac.compare_digest(
                lease.token_digest, hashlib.sha256(token.encode()).hexdigest()
            )
        ):
            raise history_access.denied()
        lease.expires_at = max(_utc(lease.expires_at), expires_at)
    for binding, previous in sources:
        if previous is None:
            db.add(
                AgentHistorySource(
                    session_id=lease.id,
                    source_binding_id=binding.id,
                    source_conversation_id=binding.openhands_conversation_id,
                    source_runtime_session_id=binding.runtime_session_id,
                    expires_at=expires_at,
                )
            )
        else:
            previous.expires_at = max(_utc(previous.expires_at), expires_at)
    db.flush()
    return lease, token


def authenticate(db: Session, token: str) -> AgentHistorySession:
    owner, identity = history_access.token_identity(token)
    if not token.startswith("fwh2."):
        raise history_access.denied()
    with tenant_user(owner):
        lease = db.get(AgentHistorySession, identity)
        if (
            lease is None
            or lease.owner_user_id != owner
            or lease.revoked_at is not None
            or _utc(lease.expires_at) <= datetime.now(UTC)
            or not hmac.compare_digest(
                lease.token_digest, hashlib.sha256(token.encode()).hexdigest()
            )
            or agent_session_principal(db, owner) is None
        ):
            raise history_access.denied()
        try:
            target, _ = conversations.history_reference_bindings(
                db, lease.workspace_id, lease.target_binding_id, lease.target_binding_id
            )
        except DomainError:
            raise history_access.denied() from None
        _assert_binding(
            target,
            owner,
            lease.workspace_id,
            lease.target_conversation_id,
            lease.target_runtime_session_id,
            allow_provisioning=True,
        )
        return lease


def authorize_source(db: Session, token: str, source_id: str | None) -> AuthorizedSource:
    lease = authenticate(db, token)
    if source_id is None:
        raise history_access.denied()
    with tenant_user(lease.owner_user_id):
        source = _source(db, lease.id, source_id)
        if (
            source is None
            or source.revoked_at is not None
            or _utc(source.expires_at) <= datetime.now(UTC)
        ):
            raise history_access.denied()
        try:
            _, binding = conversations.history_reference_bindings(
                db, lease.workspace_id, lease.target_binding_id, source.source_binding_id
            )
        except DomainError:
            raise history_access.denied() from None
        _assert_binding(
            binding,
            lease.owner_user_id,
            lease.workspace_id,
            source.source_conversation_id,
            source.source_runtime_session_id,
        )
        return AuthorizedSource(
            source.id, lease.owner_user_id, lease.workspace_id, source.source_binding_id
        )


def revoke(db: Session, workspace_id: str, target_id: str, source_id: str | None = None) -> None:
    target = _target(db, workspace_id, target_id)
    lease = _target_session(db, target.id)
    if lease is None or lease.workspace_id != workspace_id:
        raise DomainError("AGENT_HISTORY_SESSION_NOT_FOUND", "历史查询授权不存在", 404)
    if source_id is None:
        lease.revoked_at = datetime.now(UTC)
    else:
        source = _source(db, lease.id, source_id)
        if source is None:
            raise DomainError("AGENT_HISTORY_SOURCE_NOT_FOUND", "来源授权不存在", 404)
        source.revoked_at = datetime.now(UTC)
    db.flush()


def delete_binding_access(db: Session, binding_id: str) -> None:
    targets = select(AgentHistorySession.id).where(
        AgentHistorySession.target_binding_id == binding_id
    )
    db.execute(
        delete(AgentHistorySource).where(
            (AgentHistorySource.session_id.in_(targets))
            | (AgentHistorySource.source_binding_id == binding_id)
        )
    )
    db.execute(
        delete(AgentHistorySession).where(AgentHistorySession.target_binding_id == binding_id)
    )
