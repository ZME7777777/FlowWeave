"""Restricted history credentials, independent of platform login sessions."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationHistoryGrant
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.shared.errors import DomainError


def denied() -> DomainError:
    return DomainError("AGENT_HISTORY_ACCESS_DENIED", "历史查询凭据无效或已失效", 401)


def token_identity(token: str) -> tuple[str, str]:
    """Routing claims select a tenant; only the stored digest authenticates them."""

    try:
        prefix, owner, grant_id, nonce = token.split(".")
        if prefix != "fwh1" or not 40 <= len(nonce) <= 64:
            raise ValueError("invalid token")
        if str(UUID(owner)) != owner or str(UUID(grant_id)) != grant_id:
            raise ValueError("invalid identity")
        return owner, grant_id
    except (ValueError, AttributeError):
        raise denied() from None


def issue(
    db: Session, workspace_id: str, target_id: str, source_id: str, ttl_seconds: int = 900
) -> tuple[AgentConversationHistoryGrant, str]:
    if not 60 <= ttl_seconds <= 3600:
        raise DomainError("AGENT_HISTORY_TTL_INVALID", "授权时长必须在 60 到 3600 秒之间", 422)
    target, source = conversations.history_reference_bindings(
        db, workspace_id, target_id, source_id
    )
    owner = current_user_id()
    for binding in (target, source):
        if (
            binding.owner_user_id != owner
            or binding.lifecycle != "ACTIVE"
            or binding.host_kind != "AGENT_WORKSPACE"
            or binding.host_id != workspace_id
        ):
            raise denied()
    grant_id = str(uuid4())
    token = f"fwh1.{owner}.{grant_id}.{secrets.token_urlsafe(32)}"
    grant = AgentConversationHistoryGrant(
        id=grant_id,
        owner_user_id=owner,
        workspace_id=workspace_id,
        target_binding_id=target.id,
        source_binding_id=source.id,
        target_conversation_id=target.openhands_conversation_id,
        source_conversation_id=source.openhands_conversation_id,
        target_runtime_session_id=target.runtime_session_id,
        source_runtime_session_id=source.runtime_session_id,
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    db.add(grant)
    db.flush()
    return grant, token


def authorize(db: Session, token: str) -> AgentConversationHistoryGrant:
    owner, grant_id = token_identity(token)
    with tenant_user(owner):
        grant = db.get(AgentConversationHistoryGrant, grant_id)
        if (
            grant is None
            or grant.owner_user_id != owner
            or grant.revoked_at is not None
            or not hmac.compare_digest(
                grant.token_digest, hashlib.sha256(token.encode()).hexdigest()
            )
            or (
                grant.expires_at.astimezone(UTC)
                if grant.expires_at.tzinfo is not None
                else grant.expires_at.replace(tzinfo=UTC)
            )
            <= datetime.now(UTC)
        ):
            raise denied()
        if agent_session_principal(db, owner) is None:
            raise denied()
        try:
            target, source = conversations.history_reference_bindings(
                db, grant.workspace_id, grant.target_binding_id, grant.source_binding_id
            )
        except DomainError:
            raise denied() from None
        for binding, native_id, runtime_id in (
            (target, grant.target_conversation_id, grant.target_runtime_session_id),
            (source, grant.source_conversation_id, grant.source_runtime_session_id),
        ):
            if (
                binding.owner_user_id != owner
                or binding.lifecycle != "ACTIVE"
                or binding.host_kind != "AGENT_WORKSPACE"
                or binding.host_id != grant.workspace_id
                or binding.openhands_conversation_id != native_id
                or binding.runtime_session_id != runtime_id
            ):
                raise denied()
        return grant


def revoke(db: Session, workspace_id: str, target_id: str, grant_id: str) -> None:
    conversations.history_reference_bindings(db, workspace_id, target_id, target_id)
    grant = db.get(AgentConversationHistoryGrant, grant_id)
    if (
        grant is None
        or grant.owner_user_id != current_user_id()
        or grant.workspace_id != workspace_id
        or grant.target_binding_id != target_id
    ):
        raise DomainError("AGENT_HISTORY_GRANT_NOT_FOUND", "历史查询授权不存在", 404)
    grant.revoked_at = datetime.now(UTC)
    db.flush()
