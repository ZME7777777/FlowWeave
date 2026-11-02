"""User-owned MCP credential references, resolved only at native API boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import (
    is_builtin_capability,
    resolve_version,
)
from flowweave.modules.credentials.application.service import matches_host, matches_path
from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
from flowweave.runtime.base import RuntimeMCP, StartAttemptRequest
from flowweave.shared.credentials_crypto import decrypt_secret
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    CapabilityVersion,
    MCPAuthenticationBinding,
    MCPAuthenticationUse,
)
from flowweave.shared.schemas import MCPAuthenticationWrite


def _published(db: Session, version_id: str):
    published = resolve_version(db, version_id, include_retired=True)
    if published.package.capability_type != "MCP":
        raise DomainError("MCP_CAPABILITY_REQUIRED", "此操作仅适用于 MCP", 422)
    return published


def _editable(published) -> bool:
    config = published.runtime_config()
    return (
        not is_builtin_capability("MCP", published.package.capability_key)
        and bool(config.get("url"))
        and config.get("transport") != "stdio"
        and (config.get("auth") or {}).get("strategy") in (None, "none", "bearer")
        and not any(key.lower() == "authorization" for key in (config.get("headers") or {}))
    )


def _binding(db: Session, version_id: str) -> MCPAuthenticationBinding | None:
    return db.scalar(
        select(MCPAuthenticationBinding).where(
            MCPAuthenticationBinding.capability_version_id == version_id
        )
    )


def _credential(db: Session, credential_id: str, config: dict[str, Any]) -> WebsiteCredential:
    item = db.scalar(select(WebsiteCredential).where(WebsiteCredential.id == credential_id))
    if item is None:
        raise DomainError(
            "MCP_AUTHENTICATION_UNAVAILABLE", "关联认证已删除或不可访问，请重新选择", 409
        )
    if item.auth_type != "TOKEN":
        raise DomainError("MCP_AUTHENTICATION_TYPE", "Bearer 认证需要选择 Token 类型认证", 422)
    try:
        url = urlsplit(str(config.get("url") or ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("invalid target")
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if url.port is not None:
            host += f":{url.port}"
        path = url.path or "/"
        for _ in range(4):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        if unquote(path) != path:
            raise ValueError("ambiguous encoding")
        if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("ambiguous path")
    except ValueError:
        raise DomainError(
            "MCP_AUTHENTICATION_TARGET", "MCP 服务地址无效，无法绑定认证", 422
        ) from None
    if not matches_host(item, host) or not matches_path(item, path):
        raise DomainError(
            "MCP_AUTHENTICATION_SCOPE", "所选认证的主机、端口或路径范围与 MCP 服务地址不匹配", 422
        )
    return item


def read(db: Session, version_id: str) -> dict[str, Any]:
    published = _published(db, version_id)
    item = _binding(db, version_id)
    credential = (
        db.scalar(select(WebsiteCredential).where(WebsiteCredential.id == item.credential_id))
        if item and item.credential_id
        else None
    )
    valid = True
    if item and item.strategy == "bearer":
        try:
            _credential(db, item.credential_id or "", published.runtime_config())
        except DomainError:
            valid = False
    return {
        "capability_version_id": version_id,
        "strategy": item.strategy if item else "none",
        "credential_id": item.credential_id if item else None,
        "credential_name": credential.name if credential else None,
        "row_version": item.row_version if item else 0,
        "editable": _editable(published),
        "valid": valid,
    }


def save(db: Session, version_id: str, payload: MCPAuthenticationWrite) -> dict[str, Any]:
    published = _published(db, version_id)
    if not _editable(published):
        raise DomainError("MCP_AUTHENTICATION_READ_ONLY", "此 MCP 的认证由原有配置或系统管理", 403)
    # Serialize first insertion too; all user writes preserve the shared version.
    db.scalar(select(CapabilityVersion).where(CapabilityVersion.id == version_id).with_for_update())
    item = _binding(db, version_id)
    if payload.expected_row_version != (item.row_version if item else 0):
        raise DomainError("VERSION_CONFLICT", "认证绑定已更新，请刷新后重试", 409)
    if payload.strategy == "bearer":
        _credential(db, payload.credential_id or "", published.runtime_config())
    if item is None:
        item = MCPAuthenticationBinding(capability_version_id=version_id)
        db.add(item)
        item.row_version = 1
    else:
        item.row_version += 1
    item.strategy = payload.strategy
    item.credential_id = payload.credential_id
    db.flush()
    return read(db, version_id)


def _reference(db: Session, version_id: str) -> dict[str, Any]:
    binding = _binding(db, version_id)
    if binding is None or binding.strategy == "none":
        return {"strategy": "none", "binding_row_version": binding.row_version if binding else 0}
    config = _published(db, version_id).runtime_config()
    credential = _credential(db, binding.credential_id or "", config)
    return {
        "strategy": "bearer",
        "credential_id": credential.id,
        "credential_row_version": credential.row_version,
        "binding_row_version": binding.row_version,
        "server_url_sha256": hashlib.sha256(str(config["url"]).encode()).hexdigest(),
    }


def prepare_server(
    db: Session,
    server: RuntimeMCP,
    *,
    consumer_id: str | None = None,
    probe: bool = False,
) -> tuple[RuntimeMCP, dict[str, str]]:
    version_id = server.capability_version_id
    if not version_id or is_builtin_capability("MCP", server.name):
        return server, {}
    usage = None
    if consumer_id:
        # The owning conversation/attempt lifecycle already serializes its writes.
        usage = db.scalar(
            select(MCPAuthenticationUse).where(
                MCPAuthenticationUse.consumer_id == consumer_id,
                MCPAuthenticationUse.capability_version_id == version_id,
            )
        )
    reference = usage.reference_json if usage else _reference(db, version_id)
    if reference.get("strategy") != "bearer":
        prepared, secrets = server, {}
    else:
        credential = _credential(db, str(reference["credential_id"]), server.config)
        if credential.row_version != reference["credential_row_version"]:
            raise DomainError(
                "MCP_AUTHENTICATION_CHANGED", "关联认证已更新，请新建会话使用更新后的认证", 409
            )
        actual_url_digest = hashlib.sha256(str(server.config.get("url") or "").encode()).hexdigest()
        if reference["server_url_sha256"] != actual_url_digest:
            raise DomainError("MCP_AUTHENTICATION_TARGET", "MCP 服务地址与冻结认证目标不一致", 409)
        value = decrypt_secret(credential.encrypted_secret)
        name = f"FLOWWEAVE_MCP_{version_id.replace('-', '').upper()}_TOKEN"
        # Probe has no Conversation Secret Registry; send the native auth field
        # request-only. Normal sessions receive only a native variable reference.
        prepared = replace(
            server,
            config={
                **server.config,
                "auth": {
                    "strategy": "bearer",
                    "value": value if probe else "${" + name + "}",
                },
            },
        )
        secrets = {} if probe else {name: value}
    if consumer_id:
        if usage is None:
            usage = MCPAuthenticationUse(consumer_id=consumer_id, capability_version_id=version_id)
            db.add(usage)
        usage.reference_json = dict(reference)
        db.flush()
    return prepared, secrets


def prepare_request(
    db: Session, request: StartAttemptRequest, consumer_id: str
) -> StartAttemptRequest:
    if request.agent_spec is None:
        return request
    servers = []
    secrets = dict(request.conversation_secrets)
    for server in request.agent_spec.mcp_servers:
        prepared, values = prepare_server(db, server, consumer_id=consumer_id)
        servers.append(prepared)
        secrets.update(values)
    return replace(
        request,
        agent_spec=replace(request.agent_spec, mcp_servers=tuple(servers)),
        conversation_secrets=secrets,
    )


def prepare_material(
    db: Session, material: dict[str, Any], consumer_id: str
) -> tuple[dict[str, Any], dict[str, str]]:
    if material.get("capability_type") != "MCP":
        return material, {}
    server, secrets = prepare_server(
        db,
        RuntimeMCP(
            name=str(material["capability_key"]),
            config=dict(material["normalized_config"]),
            capability_version_id=str(material["capability_version_id"]),
        ),
        consumer_id=consumer_id,
    )
    return {**material, "normalized_config": server.config}, secrets
