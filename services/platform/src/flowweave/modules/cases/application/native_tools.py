"""Native MCP registration and server-derived authority for Case coordinators."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkDirectoryVersion
from flowweave.modules.cases.infrastructure.models import Case, CaseControlRound, CaseToolGrant
from flowweave.modules.catalog.public import PublishedCapability, resolve_version
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

SERVER_NAME = "flowweave-case-context"
SECRET_NAME = "FLOWWEAVE_CASE_ACCESS"
AUTH_HEADER = "Bearer ${FLOWWEAVE_CASE_ACCESS}"
MCP_PATH = "/api/v1/case-tools-mcp"


def denied() -> DomainError:
    return DomainError("CASE_TOOL_ACCESS_DENIED", "场景工具授权无效或已失效", 401)


def unavailable() -> DomainError:
    return DomainError("CASE_TOOL_CAPABILITY_UNAVAILABLE", "场景工具未配置或冻结版本无效", 409)


def token_identity(token: str) -> tuple[str, str]:
    # Claims only route RLS. The stored digest below authenticates the credential.
    try:
        prefix, owner, grant_id, nonce = token.split(".")
        if prefix != "fwc1" or not 40 <= len(nonce) <= 64:
            raise ValueError
        if str(UUID(owner)) != owner or str(UUID(grant_id)) != grant_id:
            raise ValueError
        return owner, grant_id
    except (ValueError, AttributeError):
        raise denied() from None


def _scope(db: Session, round_id: str, binding_id: str) -> tuple[CaseControlRound, Case]:
    owner = current_user_id()
    entry = db.get(CaseControlRound, round_id, populate_existing=True)
    item = db.get(Case, entry.case_id, populate_existing=True) if entry else None
    binding = db.get(AgentConversationBinding, binding_id, populate_existing=True)
    directory = (
        db.get(AgentWorkDirectoryVersion, binding.work_directory_version_id) if binding else None
    )
    if (
        entry is None
        or item is None
        or binding is None
        or directory is None
        or entry.owner_user_id != owner
        or item.owner_user_id != owner
        or binding.owner_user_id != owner
        or directory.owner_user_id != owner
        or entry.binding_id != binding.id
        or item.current_control_round_id != entry.id
        or item.control_conversation_binding_id != binding.id
        or entry.state not in {"PENDING", "STARTED"}
        or item.state in {"COMPLETED", "ARCHIVED"}
        or binding.host_kind != "AGENT_WORKSPACE"
        or binding.host_id != entry.workspace_id
        or binding.workspace_id != entry.workspace_id
        or binding.lifecycle not in {"ACTIVE", "PROVISIONING"}
        or directory.work_directory_id != entry.work_directory_id
        or item.control_work_directory_id != entry.work_directory_id
        or agent_session_principal(db, owner) is None
    ):
        raise denied()
    return entry, item


def authenticate(db: Session, token: str) -> tuple[CaseToolGrant, Case]:
    owner, grant_id = token_identity(token)
    with tenant_user(owner):
        grant = db.get(CaseToolGrant, grant_id, populate_existing=True)
        if (
            grant is None
            or grant.owner_user_id != owner
            or not hmac.compare_digest(
                grant.token_digest, hashlib.sha256(token.encode()).hexdigest()
            )
        ):
            raise denied()
        _, item = _scope(db, grant.round_id, grant.binding_id)
        binding = db.get(AgentConversationBinding, grant.binding_id)
        frozen = db.scalar(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == grant.binding_id,
                AgentConversationCapability.capability_key == SERVER_NAME,
            )
        )
        if (
            binding is None
            or binding.work_directory_version_id != grant.work_directory_version_id
            or binding.runtime_session_id != grant.runtime_session_id
            or binding.openhands_conversation_id != grant.native_conversation_id
            or frozen is None
            or frozen.capability_version_id != grant.capability_version_id
            or frozen.digest != grant.capability_digest
        ):
            raise denied()
        return grant, item


def _published(db: Session, version_id: str | None = None) -> PublishedCapability:
    settings = get_settings()
    configured_id = version_id or settings.case_tools_mcp_capability_version_id
    try:
        if not configured_id:
            raise unavailable()
        published = resolve_version(db, configured_id)
        config = published.version.normalized_config_json
        url = config.get("url", "")
        if not isinstance(url, str):
            raise unavailable()
        parsed = urlsplit(url)
        if (
            published.package.capability_type != "MCP"
            or published.package.capability_key != SERVER_NAME
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(MCP_PATH)
            or (version_id is None and url != settings.case_tools_mcp_url)
            or config.get("transport") != "streamable-http"
            or config.get("headers") != {"Authorization": AUTH_HEADER}
            or set(config) - {"url", "transport", "headers"}
            or published.version.digest
            != capability_version_digest("MCP", SERVER_NAME, published.blob.content_hash, config)
        ):
            raise unavailable()
        content = get_artifact_store().read(published.blob.storage_key)
        if hashlib.sha256(content).hexdigest() != published.blob.content_hash:
            raise unavailable()
        return published
    except (DomainError, OSError, ValueError, TypeError):
        raise unavailable() from None


def prepare_creation(
    db: Session, round_id: str, binding: AgentConversationBinding
) -> dict[str, str]:
    """Commit a frozen reference and scoped grant before native MCP initialization.

    Called only by Case bootstrap. Generic sessions never receive Case authority.
    Retry keeps the same credential and version, even if defaults have changed.
    """
    entry, _ = _scope(db, round_id, binding.id)
    grant = db.scalar(select(CaseToolGrant).where(CaseToolGrant.round_id == entry.id))
    published = _published(db, grant.capability_version_id if grant else None)
    reference = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id,
            AgentConversationCapability.capability_key == SERVER_NAME,
        )
    )
    if reference is not None and (
        reference.capability_version_id != published.version.id
        or reference.digest != published.version.digest
    ):
        raise unavailable()
    if grant is None:
        grant_id = str(uuid4())
        token = f"fwc1.{entry.owner_user_id}.{grant_id}.{secrets.token_urlsafe(32)}"
        grant = CaseToolGrant(
            id=grant_id,
            owner_user_id=entry.owner_user_id,
            round_id=entry.id,
            binding_id=binding.id,
            work_directory_version_id=binding.work_directory_version_id,
            runtime_session_id=binding.runtime_session_id,
            native_conversation_id=binding.openhands_conversation_id,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            encrypted_token=encrypt_secret(token),
            capability_version_id=published.version.id,
            capability_digest=published.version.digest,
            runtime_manifest_json={
                "schema_version": 1,
                "capabilities": [
                    {
                        "type": "MCP",
                        "key": SERVER_NAME,
                        "version_id": published.version.id,
                        "digest": published.version.digest,
                        "blob_id": published.blob.id,
                        "content_hash": published.blob.content_hash,
                    }
                ],
            },
        )
        db.add(grant)
    if reference is None:
        position = db.scalar(
            select(func.max(AgentConversationCapability.position)).where(
                AgentConversationCapability.binding_id == binding.id
            )
        )
        db.add(
            AgentConversationCapability(
                binding_id=binding.id,
                capability_type="MCP",
                capability_key=SERVER_NAME,
                capability_version_id=published.version.id,
                digest=published.version.digest,
                position=(position if position is not None else -1) + 1,
            )
        )
    db.flush()
    token = decrypt_secret(grant.encrypted_token)
    authenticate(db, token)
    db.commit()
    return {SECRET_NAME: token}


def assert_native_registration(db: Session, binding_id: str, handle: RuntimeHandle) -> None:
    """Recovery may adopt a native conversation only with its frozen Case MCP."""
    grant = db.scalar(select(CaseToolGrant).where(CaseToolGrant.binding_id == binding_id))
    if grant is None:
        raise unavailable()
    published = _published(db, grant.capability_version_id)
    if not get_runtime().conversation_mcp_configured(
        handle, SERVER_NAME, published.version.normalized_config_json["url"]
    ):
        # Preserve the existing native identity for reconciliation. Deleting it
        # as a definitively failed reservation would discard unknown history.
        raise DomainError(
            "CASE_TOOL_REGISTRATION_MISMATCH",
            "原生场景工具配置需要核对，请保留原轮次重试",
            409,
            {"outcome_unknown": True},
        )
