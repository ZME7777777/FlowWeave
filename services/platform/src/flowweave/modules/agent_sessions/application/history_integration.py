"""Governed native MCP registration before delivering metadata-only whole references."""

import hashlib
import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations, history_sessions
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationCapability,
    AgentHistorySession,
)
from flowweave.modules.catalog.public import PublishedCapability, resolve_version
from flowweave.runtime.base import RuntimeHandle, RuntimeMCP, RuntimeMCPProbeRequest
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.workspace import (
    materialize_agent_workspace_capability_marketplace,
    register_agent_workspace_capability_manifest,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.credentials_crypto import decrypt_secret
from flowweave.shared.database import now
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

SERVER_NAME = "flowweave-reference-history"
SECRET_NAME = "FLOWWEAVE_HISTORY_ACCESS"
AUTH_HEADER = "Bearer ${FLOWWEAVE_HISTORY_ACCESS}"
_logger = logging.getLogger(__name__)


def unavailable(reason: str = "CONFIGURATION_INVALID") -> DomainError:
    explanation = {
        "MCP_TIMEOUT": "连接超时",
        "MCP_CONNECTION": "连接或认证失败",
        "MCP_UNKNOWN": "连接检测失败",
        "TOOLS_MISSING": "服务未返回历史查询工具",
        "NATIVE_CONFIGURATION_MISSING": "运行环境未确认查询能力配置",
        "CONFIGURATION_INVALID": "发布配置或版本校验失败",
    }.get(reason, "运行环境暂不可用")
    _logger.warning("History MCP unavailable: reason=%s", reason)
    return DomainError(
        "AGENT_HISTORY_CAPABILITY_UNAVAILABLE",
        f"会话历史查询能力不可用：{explanation}；请在能力仓库测试连接后重试，引用和草稿已保留",
        409,
        {"reason": reason},
    )


def configured_capability(db: Session) -> PublishedCapability:
    settings = get_settings()
    url = settings.agent_history_mcp_url
    parsed = urlsplit(url)
    if (
        not settings.agent_history_mcp_capability_version_id
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/api/v1/agent-history-mcp")
    ):
        raise unavailable()
    published = resolve_version(db, settings.agent_history_mcp_capability_version_id)
    config = published.version.normalized_config_json
    if (
        published.package.capability_type != "MCP"
        or published.package.capability_key != SERVER_NAME
        or config.get("url") != url
        or config.get("transport") != "streamable-http"
        or config.get("headers") != {"Authorization": AUTH_HEADER}
        or config.get("enabled") is False
        or set(config)
        - {
            "url",
            "transport",
            "headers",
            "description",
            "icon",
            "timeout",
            "sse_read_timeout",
            "keep_alive",
            "enabled",
        }
        or published.version.digest
        != capability_version_digest("MCP", SERVER_NAME, published.blob.content_hash, config)
    ):
        raise unavailable()
    try:
        content = get_artifact_store().read(published.blob.storage_key)
    except (FileNotFoundError, OSError):
        raise unavailable() from None
    if hashlib.sha256(content).hexdigest() != published.blob.content_hash:
        raise unavailable()
    return published


def source_ids(references: tuple[dict[str, str], ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item["source_binding_id"]
            for item in references
            if item.get("kind") == "conversation" and not item.get("resolved_content")
        )
    )


def probe(handle: RuntimeHandle, published: PublishedCapability, token: str) -> None:
    config = dict(published.version.normalized_config_json)
    config["headers"] = {"Authorization": f"Bearer {token}"}
    result = get_runtime().probe_mcp(
        RuntimeMCPProbeRequest(
            server=RuntimeMCP(name=SERVER_NAME, config=config),
            base_url=f"http://{handle.runtime_resource_name}:8000",
            runtime_resource_name=handle.runtime_resource_name,
            timeout=10,
        )
    )
    if not result.ok:
        raise unavailable(f"MCP_{(result.error_kind or 'unknown').upper()}")
    if "read_referenced_conversation" not in result.tools:
        raise unavailable("TOOLS_MISSING")


def prepare_creation(
    db: Session, workspace: Any, target: Any, references: tuple[dict[str, str], ...]
) -> None:
    """Freeze initial native MCP input and commit its authority before Server initialization."""
    sources = source_ids(references)
    if not sources:
        return
    published = configured_capability(db)
    lease, token = history_sessions.renew(
        db, workspace.id, target.id, sources, 3600, allow_provisioning=True
    )
    if lease.capability_version_id not in {
        None,
        published.version.id,
    } or lease.capability_digest not in {None, published.version.digest}:
        raise unavailable()
    lease.capability_version_id = published.version.id
    lease.capability_digest = published.version.digest
    freeze_reference(db, target.id, published)
    db.commit()
    probe(conversations.history_runtime_handle(db, workspace, target), published, token)


def creation_secrets(db: Session, target: Any) -> dict[str, str]:
    frozen = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == target.id,
            AgentConversationCapability.capability_key == SERVER_NAME,
        )
    )
    if frozen is None:
        return {}
    lease = db.scalar(
        select(AgentHistorySession).where(AgentHistorySession.target_binding_id == target.id)
    )
    if (
        lease is None
        or lease.capability_version_id != frozen.capability_version_id
        or lease.capability_digest != frozen.digest
    ):
        raise unavailable()
    token = decrypt_secret(lease.encrypted_token)
    history_sessions.authenticate(db, token)
    return {SECRET_NAME: token}


def freeze_reference(db: Session, binding_id: str, published: PublishedCapability) -> None:
    reference = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding_id,
            AgentConversationCapability.capability_key == SERVER_NAME,
        )
    )
    if reference is not None:
        if (
            reference.capability_version_id != published.version.id
            or reference.digest != published.version.digest
        ):
            raise unavailable()
        return
    position = db.scalar(
        select(func.max(AgentConversationCapability.position)).where(
            AgentConversationCapability.binding_id == binding_id
        )
    )
    db.add(
        AgentConversationCapability(
            binding_id=binding_id,
            capability_version_id=published.version.id,
            capability_type="MCP",
            capability_key=SERVER_NAME,
            digest=published.version.digest,
            position=(position if position is not None else -1) + 1,
        )
    )
    db.flush()


def ensure(
    db: Session,
    workspace: Any,
    target: Any,
    references: tuple[dict[str, str], ...],
    *,
    allow_install: bool = True,
) -> bool:
    try:
        return _ensure(db, workspace, target, references, allow_install=allow_install)
    except DomainError as exc:
        if exc.code.startswith("AGENT_HISTORY_"):
            raise
        raise unavailable(exc.code) from exc


def _ensure(
    db: Session,
    workspace: Any,
    target: Any,
    references: tuple[dict[str, str], ...],
    *,
    allow_install: bool = True,
) -> bool:
    """Renew before a send; install once at an idle boundary and commit grants before MCP I/O.

    Binding row locks serialize registration. A persisted native configuration
    acknowledges a loader whose response was lost, without loading it twice.
    No plaintext credential is written to the version, blob or runtime manifest.
    """
    sources = source_ids(references)
    if not sources:
        return True
    published = configured_capability(db)
    existing = db.scalar(
        select(AgentHistorySession).where(AgentHistorySession.target_binding_id == target.id)
    )
    had_intent = existing is not None and existing.capability_version_id == published.version.id
    if not allow_install and (existing is None or existing.registered_at is None):
        return False
    runtime = get_runtime()
    handle = conversations.history_runtime_handle(db, workspace, target)
    if existing is None or existing.registered_at is None:
        if not runtime.can_accept_input(handle):
            raise DomainError(
                "AGENT_HISTORY_CAPABILITY_REQUIRES_IDLE",
                "首次引用会话需要加载查询能力，请等待当前回复完成后重试",
                409,
            )
    if target.lifecycle == "PROVISIONING":
        identity = runtime.reload_conversation(handle)
        if identity.conversation_id != target.openhands_conversation_id:
            raise unavailable()
    lease, token = history_sessions.renew(
        db,
        workspace.id,
        target.id,
        sources,
        3600,
        allow_provisioning=target.lifecycle == "PROVISIONING",
    )
    if lease.capability_version_id not in {
        None,
        published.version.id,
    } or lease.capability_digest not in {None, published.version.digest}:
        raise DomainError(
            "AGENT_HISTORY_CAPABILITY_VERSION_CONFLICT",
            "此会话已冻结其他历史查询版本，请使用新会话",
            409,
        )
    material = {
        "capability_type": "MCP",
        "capability_key": SERVER_NAME,
        "capability_version_id": published.version.id,
        "digest": published.version.digest,
        "normalized_config": published.runtime_config(),
    }
    lease.capability_version_id = published.version.id
    lease.capability_digest = published.version.digest
    lease.runtime_manifest_json = {
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
    }
    # A native MCP initialize request must see its committed authority. It
    # cannot authenticate through an uncommitted control-plane transaction.
    db.commit()
    lease_id = lease.id
    if existing is None or existing.registered_at is None:
        probe(handle, published, token)
    db.expire_all()
    conversations.history_reference_bindings(
        db, workspace.id, target.id, target.id, lock_target=True
    )
    lease = db.get(AgentHistorySession, lease_id, populate_existing=True)
    if lease is None or lease.revoked_at is not None:
        raise unavailable()
    history_sessions.authenticate(db, token)
    handle = conversations.history_runtime_handle(db, workspace, target)
    if lease.registered_at is not None:
        return True
    host_root, runtime_root, marketplace = conversations.history_capability_marketplace_paths(
        db, workspace, target
    )
    plugin = materialize_agent_workspace_capability_marketplace(
        material, host_root=host_root, runtime_root=runtime_root, marketplace_name=marketplace
    )
    if plugin is None:
        raise unavailable()
    configured = runtime.conversation_mcp_configured(
        handle, SERVER_NAME, get_settings().agent_history_mcp_url
    )
    if configured and not had_intent:
        raise DomainError(
            "AGENT_HISTORY_CAPABILITY_VERSION_CONFLICT",
            "此会话存在未关联的历史查询配置，请使用新会话",
            409,
        )
    if not configured:
        runtime.update_conversation_secrets(handle, {SECRET_NAME: token})
        runtime.load_plugin(handle, f"{plugin}@{marketplace}")
        if not runtime.conversation_mcp_configured(
            handle, SERVER_NAME, get_settings().agent_history_mcp_url
        ):
            raise unavailable("NATIVE_CONFIGURATION_MISSING")
    # This records native configuration acknowledgement, not model execution
    # or a fabricated live-tool status. Actual tool use remains native MCP.
    freeze_reference(db, target.id, published)
    register_agent_workspace_capability_manifest(
        material, host_root=host_root, runtime_root=runtime_root
    )
    lease.registered_at = now()
    db.flush()
    db.commit()
    return True
