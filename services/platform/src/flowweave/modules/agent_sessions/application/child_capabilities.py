"""Governed dynamic capability registration in one persistent native child."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import child_sessions as children
from flowweave.modules.agent_sessions.application import conversations, flow_node_conversations
from flowweave.modules.agent_sessions.application.capability_versions import (
    current_capabilities,
    validate_skill_upgrade,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspace
from flowweave.modules.catalog.application import mcp_authentication
from flowweave.runtime.base import RuntimeMCPProbeRequest
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.workspace import (
    materialize_agent_workspace_capabilities,
    materialize_agent_workspace_capability_marketplace,
)
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings


@contextmanager
def registration_guard(parent_id: str, child_id: str) -> Iterator[None]:
    # The shared storage lock survives request cancellation until the worker
    # finishes native I/O and projection. It holds no database connection.
    root = Path(get_settings().workspace_root) / ".child-capability-locks"
    root.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(f"{parent_id}/{child_id}".encode()).hexdigest()
    descriptor = os.open(root / identity, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError(
                "AGENT_CONVERSATION_BUSY", "子会话正在加载能力，请稍候重试", 409
            ) from None
        yield


def marketplace_paths(db: Session, loc: children.ChildLocator) -> tuple[Path, Path, str]:
    parent = db.get(AgentConversationBinding, loc.parent_id)
    if parent is None:
        raise children.unavailable()
    if loc.host_kind == "AGENT_WORKSPACE":
        workspace = db.get(AgentWorkspace, loc.host_id)
        if workspace is None:
            raise children.unavailable()
        return conversations.history_capability_marketplace_paths(db, workspace, parent)
    return flow_node_conversations.node_capability_marketplace_paths(
        db,
        flow_run_id=loc.host_id,
        attempt_id=loc.scope_id,
        binding_id=parent.id,
    )


@dataclass(frozen=True)
class CapabilityLoad:
    version_id: str
    capability_type: str
    capability_key: str
    digest: str
    material: dict[str, Any] = field(repr=False)
    secrets: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class Registration:
    locator: children.ChildLocator
    host_root: Path
    runtime_root: Path
    marketplace_name: str
    pending: tuple[CapabilityLoad, ...]
    already_loaded: tuple[str, ...]


def prepare_registration(
    db: Session,
    parent_id: str,
    child_id: str,
    version_ids: tuple[str, ...],
) -> Registration:
    loc = children.resolve(db, parent_id, child_id, write=True)
    selected = conversations.validated_session_capabilities(db, version_ids)
    existing = current_capabilities(
        db.scalars(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == child_id,
            )
        )
    )
    versions = {item.capability_version_id for item in existing}
    names = {(item.capability_type, item.capability_key): item for item in existing}
    pending: list[CapabilityLoad] = []
    already_loaded: list[str] = []
    for published, kind in selected:
        if published.version.id in versions:
            already_loaded.append(published.version.id)
            continue
        previous = names.get((kind, published.package.capability_key))
        if previous is not None:
            validate_skill_upgrade(db, previous, published)
        material, secrets = mcp_authentication.prepare_material(
            db,
            conversations.frozen_runtime_capability(db, published, kind),
            f"conversation:{child_id}",
        )
        pending.append(
            CapabilityLoad(
                published.version.id,
                kind,
                published.package.capability_key,
                published.version.digest,
                material,
                secrets,
            )
        )
    host_root, runtime_root, name = marketplace_paths(db, loc)
    return Registration(loc, host_root, runtime_root, name, tuple(pending), tuple(already_loaded))


def load_registration(plan: Registration) -> tuple[list[CapabilityLoad], dict[str, str] | None]:
    configuration = children.verify_native(plan.locator)
    if not plan.pending:
        return [], None
    runtime = get_runtime()
    if not runtime.can_accept_input(plan.locator.handle):
        raise DomainError("AGENT_CONVERSATION_RUNNING", "会话运行中，完成当前回复后再加载能力", 409)
    context: dict[str, Any] = configuration.agent.get("agent_context") or {}
    registrations: list[dict[str, Any]] = context.get("registered_marketplaces") or []
    expected_source = str(plan.runtime_root / "marketplace")
    if not any(
        entry.get("name") == plan.marketplace_name
        and entry.get("source") == expected_source
        and entry.get("auto_load") is False
        for entry in registrations
    ):
        raise DomainError(
            "AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE",
            "子会话未继承可动态加载的能力市场",
            409,
        )
    loaded: list[CapabilityLoad] = []
    for item in plan.pending:
        try:
            if item.secrets:
                runtime.update_conversation_secrets(plan.locator.handle, item.secrets)
            plugin = materialize_agent_workspace_capability_marketplace(
                item.material,
                host_root=plan.host_root,
                runtime_root=plan.runtime_root,
                marketplace_name=plan.marketplace_name,
                plugin_namespace=f"child-{plan.locator.binding_id}",
            )
            if plugin is None:
                raise DomainError("RUNTIME_CAPABILITY_UNAVAILABLE", "能力插件物化失败", 409)
            runtime.load_plugin(plan.locator.handle, f"{plugin}@{plan.marketplace_name}")
        except DomainError as exc:
            return loaded, {
                "capability_version_id": item.version_id,
                "code": exc.code,
                "message": exc.message,
            }
        loaded.append(item)
    return loaded, None


def project_registration(
    db: Session,
    plan: Registration,
    result: tuple[list[CapabilityLoad], dict[str, str] | None],
) -> dict[str, Any]:
    binding = children.mutation_binding(db, plan.locator)
    loaded, failure = result
    existing = list(
        db.scalars(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == binding.id,
            )
        )
    )
    position = max((item.position for item in existing), default=-1)
    for item in loaded:
        position += 1
        db.add(
            AgentConversationCapability(
                binding_id=binding.id,
                capability_version_id=item.version_id,
                capability_type=item.capability_type,
                capability_key=item.capability_key,
                digest=item.digest,
                position=position,
            )
        )
    if loaded:
        binding.updated_at = now()
    db.flush()
    return {
        "conversation": children.view(db, plan.locator),
        "completed": failure is None,
        "loaded_capability_version_ids": [item.version_id for item in loaded],
        "already_loaded_capability_version_ids": list(plan.already_loaded),
        "failure": failure,
    }


@dataclass(frozen=True)
class MCPProbe:
    locator: children.ChildLocator
    material: dict[str, Any] = field(repr=False)
    auth: dict[str, Any] | None = field(repr=False)
    host_root: Path
    runtime_root: Path


def prepare_probe(db: Session, parent_id: str, child_id: str, version_id: str) -> MCPProbe:
    loc = children.resolve(db, parent_id, child_id)
    ((published, kind),) = conversations.validated_session_capabilities(db, (version_id,))
    if kind != "MCP":
        raise DomainError("MCP_CAPABILITY_REQUIRED", "只能检测 MCP 能力", 422)
    material = conversations.frozen_runtime_capability(db, published, kind)
    from flowweave.runtime.base import RuntimeMCP

    server, _ = mcp_authentication.prepare_server(
        db,
        RuntimeMCP(
            name=published.package.capability_key,
            config=dict(material["normalized_config"]),
            capability_version_id=published.version.id,
        ),
        probe=True,
    )
    host_root, runtime_root, _ = marketplace_paths(db, loc)
    probe_path = Path("child-probes") / child_id / str(uuid4())
    return MCPProbe(
        loc, material, server.config.get("auth"), host_root / probe_path, runtime_root / probe_path
    )


def probe_mcp(plan: MCPProbe) -> dict[str, Any]:
    children.verify_native(plan.locator)
    try:
        _, _, servers = materialize_agent_workspace_capabilities(
            (plan.material,),
            host_root=plan.host_root,
            runtime_root=plan.runtime_root,
        )
        resource_name = plan.locator.handle.runtime_resource_name
        if not resource_name:
            raise children.unavailable()
        server = servers[0]
        if plan.auth is not None:
            server = replace(server, config={**server.config, "auth": plan.auth})
        result = get_runtime().probe_mcp(
            RuntimeMCPProbeRequest(
                server=server,
                base_url=f"http://{resource_name}:8000",
                runtime_resource_name=resource_name,
            )
        )
        return {
            "state": "READY" if result.ok else "UNAVAILABLE",
            "error_kind": result.error_kind,
            "tools": list(result.tools),
            "checked_at": now().isoformat(),
        }
    except DomainError as exc:
        return {
            "state": "UNAVAILABLE",
            "error_kind": "unknown",
            "reason": exc.code,
            "tools": [],
            "checked_at": now().isoformat(),
        }
    finally:
        shutil.rmtree(plan.host_root, ignore_errors=True)
