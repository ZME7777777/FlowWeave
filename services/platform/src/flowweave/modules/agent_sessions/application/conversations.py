from __future__ import annotations

import base64
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.runtime_config import (
    build_agent_spec,
    config_from_binding,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.catalog.public import resolve_version
from flowweave.modules.model_providers.public import has_connected_default_model
from flowweave.modules.sandboxes.public import ManagedSandbox
from flowweave.modules.tasks.public import enqueue
from flowweave.runtime.base import (
    RuntimeCondenser,
    RuntimeHandle,
    RuntimeMCPProbeRequest,
    RuntimeProvider,
    StartAttemptRequest,
)
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.request import runtime_provider
from flowweave.runtime.workspace import (
    agent_workspace_capability_marketplace_name,
    materialize_agent_workspace_capabilities,
    materialize_agent_workspace_capability_marketplace,
)
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.settings import get_settings

_PROJECT_ROOT = "/runtime/workspace/project"
_PROACTIVE_COMPACTION_RATIO = 0.8
_AGENT_WORKSPACE_CONDENSER_MAX_EVENTS = 10_000
_DYNAMIC_CAPABILITY_TYPES = frozenset({"SKILL", "MCP", "PLUGIN"})
_CREATION_CAPABILITY_TYPES = _DYNAMIC_CAPABILITY_TYPES | {"CONTEXT"}
_COMPACTION_EVENT_WAIT_SECONDS = 120.0
_SANDBOX_PROJECT_IMAGE = re.compile(
    r"sandbox:(/runtime/workspace/project/[A-Za-z0-9][A-Za-z0-9._/-]*)"
)
_MECHANICAL_TITLE = re.compile(
    r"^(?:未命名会话|新会话)\s*(?:[0-9]+|[一二三四五六七八九十]+)?$",
    re.IGNORECASE,
)
_PROJECT_ROOT_SYSTEM_CONTEXT = "\n".join(
    (
        "当前会话的项目根目录是 /runtime/workspace/project。",
        "所有需要保留的代码、配置、文档和用户产物必须写入该目录或其子目录。",
        "可按需求或功能自行创建子目录；优先使用相对于项目根的路径。",
        "不要将用户项目文件写入项目根以外的位置，例如 /runtime 的其他目录、/tmp 或 HOME。",
        "不要向用户解释宿主机路径、Docker 挂载或容器实现细节；对用户而言，这就是项目根目录。",
        "多步骤任务必须使用原生任务跟踪器维护目标、未完成项和完成条件；压缩上下文后继续执行时，不得把最近一次局部结果误当成用户的最终目标。",
        "只要任务跟踪器仍有未完成项，或用户的完成条件尚未满足，就不得因为上下文压缩而提前收口。",
    )
)

# The default Agent Workspace is a host adapter.  Keep its compatibility ORM
# aliases local so the shared session implementation never imports its private
# application or infrastructure modules.
AgentWorkDirectoryVersion = agent_workspace_host.AgentWorkDirectoryVersion
AgentWorkspace = agent_workspace_host.AgentWorkspace
AgentWorkspaceCapability = agent_workspace_host.AgentWorkspaceCapability
AgentWorkspaceRuntime = agent_workspace_host.AgentWorkspaceRuntime


def _workspace(db: Session, workspace_id: str) -> AgentWorkspace:
    item = db.get(AgentWorkspace, workspace_id)
    if item is None:
        raise not_found("agent_workspace", workspace_id)
    return item


def _project_sandbox_images(content: str, *, workspace_id: str, binding_id: str) -> str:
    """Project Agent-local images through the authenticated workspace file route.

    OpenHands messages may refer to a file produced in its container with a
    ``sandbox:`` URL. That protocol is meaningful only to the Runtime, not a
    browser. Preserve the native message as the source of truth while safely
    projecting project-root image paths to the existing, scope-checked file
    endpoint. Other sandbox URLs remain untouched and therefore cannot grant
    browser access to arbitrary Runtime files.
    """

    def replace_url(match: re.Match[str]) -> str:
        query = urlencode({"path": match.group(1), "binding_id": binding_id})
        return f"/api/v1/agent-workspaces/{workspace_id}/workspace/file?{query}"

    return _SANDBOX_PROJECT_IMAGE.sub(replace_url, content)


def _binding(
    db: Session, workspace_id: str, binding_id: str, *, lock: bool = False
) -> AgentConversationBinding:
    query = select(AgentConversationBinding).where(
        AgentConversationBinding.id == binding_id,
        AgentConversationBinding.workspace_id == workspace_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None or item.lifecycle == "DELETED":
        raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除", 404)
    return item


def _dict(db: Session, item: AgentConversationBinding) -> dict[str, Any]:
    work_directory_id = (
        db.scalar(
            select(AgentWorkDirectoryVersion.work_directory_id).where(
                AgentWorkDirectoryVersion.id == item.work_directory_version_id
            )
        )
        if item.work_directory_version_id
        else None
    )
    return {
        "id": item.id,
        "display_title": item.display_title,
        "title_state": item.title_state,
        "model_provider_id": item.model_provider_id,
        "model_name": item.model_name,
        "reasoning_effort": item.reasoning_effort,
        "work_directory_version_id": item.work_directory_version_id,
        "work_directory_id": work_directory_id,
        "working_directory": item.working_directory,
        "capabilities": [
            {
                "id": capability.capability_version_id,
                "capability_type": capability.capability_type,
                "capability_key": capability.capability_key,
                "digest": capability.digest,
            }
            for capability in db.scalars(
                select(AgentConversationCapability)
                .where(AgentConversationCapability.binding_id == item.id)
                .order_by(AgentConversationCapability.position)
            )
        ],
        "streaming_callback_ready": item.streaming_callback_ready,
        "lifecycle": item.lifecycle,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "last_connected_at": item.last_connected_at.isoformat() if item.last_connected_at else None,
    }


def _workspace_dict(workspace: AgentWorkspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "display_name": workspace.display_name,
        "default_model_provider_id": workspace.default_model_provider_id,
        "desired_state": workspace.desired_state,
        "updated_at": workspace.updated_at.isoformat(),
    }


def _capability_dict(
    reference: AgentWorkspaceCapability | AgentConversationCapability,
) -> dict[str, str]:
    return {
        "id": reference.capability_version_id,
        "capability_type": reference.capability_type,
        "capability_key": reference.capability_key,
        "digest": reference.digest,
    }


def workspace_capabilities(db: Session, workspace_id: str) -> list[dict[str, str]]:
    _workspace(db, workspace_id)
    return [
        _capability_dict(reference)
        for reference in db.scalars(
            select(AgentWorkspaceCapability)
            .where(AgentWorkspaceCapability.workspace_id == workspace_id)
            .order_by(AgentWorkspaceCapability.position)
        )
    ]


def _validated_capabilities(
    db: Session,
    capability_version_ids: tuple[str, ...],
    *,
    allowed_types: frozenset[str] = _DYNAMIC_CAPABILITY_TYPES,
) -> tuple[tuple[Any, ...], ...]:
    if len(capability_version_ids) > 30:
        raise DomainError("AGENT_WORKSPACE_CAPABILITY_LIMIT", "最多启用 30 项能力", 422)
    selected: list[tuple[Any, ...]] = []
    seen_versions: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    for version_id in capability_version_ids:
        if version_id in seen_versions:
            raise DomainError("AGENT_CONVERSATION_CAPABILITY_DUPLICATE", "能力不能重复选择", 422)
        published = resolve_version(db, version_id)
        capability_type = published.package.capability_type
        identity = (capability_type, published.package.capability_key)
        if capability_type not in allowed_types:
            raise DomainError(
                "AGENT_CONVERSATION_CAPABILITY_UNSUPPORTED",
                "Agent 会话不支持该能力类型",
                422,
                {"capability_version_id": version_id},
            )
        if identity in seen_names:
            raise DomainError(
                "AGENT_CONVERSATION_CAPABILITY_CONFLICT",
                "同类型同名称能力只能选择一个版本",
                422,
                {"capability_key": published.package.capability_key},
            )
        seen_versions.add(version_id)
        seen_names.add(identity)
        selected.append((published, capability_type))
    return tuple(selected)


def replace_workspace_capabilities(
    db: Session, workspace_id: str, capability_version_ids: tuple[str, ...]
) -> list[dict[str, str]]:
    workspace = _workspace(db, workspace_id)
    selected = _validated_capabilities(db, capability_version_ids)
    for reference in db.scalars(
        select(AgentWorkspaceCapability)
        .where(AgentWorkspaceCapability.workspace_id == workspace.id)
        .with_for_update()
    ):
        db.delete(reference)
    db.flush()
    for position, (published, capability_type) in enumerate(selected):
        db.add(
            AgentWorkspaceCapability(
                workspace_id=workspace.id,
                capability_version_id=published.version.id,
                capability_type=capability_type,
                capability_key=published.package.capability_key,
                digest=published.version.digest,
                position=position,
            )
        )
    workspace.updated_at = now()
    db.flush()
    return workspace_capabilities(db, workspace.id)


def _mcp_readiness_error(error_kind: str | None, capability_keys: tuple[str, ...]) -> DomainError:
    """Return a safe, product-facing error for an MCP connection failure."""

    names = "、".join(f"「{key}」" for key in capability_keys) or "已选 MCP"
    if error_kind == "timeout":
        message = f"MCP {names} 连接超时；请在能力管理中重新检测后再发送消息。"
    elif error_kind == "connection":
        message = f"MCP {names} 无法连接；请在能力管理中重新检测后再发送消息。"
    else:
        message = f"MCP {names} 当前不可用；请在能力管理中重新检测后再发送消息。"
    return DomainError(
        "AGENT_MCP_UNAVAILABLE",
        message,
        503,
        {"error_kind": error_kind or "unknown", "capability_keys": list(capability_keys)},
    )


def _binding_mcp_keys(db: Session, binding: AgentConversationBinding) -> tuple[str, ...]:
    return tuple(
        reference.capability_key
        for reference in db.scalars(
            select(AgentConversationCapability)
            .where(
                AgentConversationCapability.binding_id == binding.id,
                AgentConversationCapability.capability_type == "MCP",
            )
            .order_by(AgentConversationCapability.position)
        )
    )


def probe_workspace_mcp_readiness(
    db: Session, workspace_id: str, capability_version_id: str
) -> dict[str, Any]:
    """Probe one published MCP from the active Agent Workspace Runtime.

    The probe deliberately does not create a Conversation.  It materializes the
    immutable package under the Workspace's read-only capability mount so its
    network and filesystem context exactly match the one used by future
    conversations.
    """

    workspace = _workspace(db, workspace_id)
    published = resolve_version(db, capability_version_id)
    if published.package.capability_type != "MCP":
        raise DomainError("MCP_CAPABILITY_REQUIRED", "只能检测 MCP 能力", 422)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None or runtime.status != "ACTIVE" or runtime.active_generation is None:
        return {"state": "UNAVAILABLE", "error_kind": "unknown", "checked_at": now().isoformat()}
    sandbox = db.scalar(
        select(ManagedSandbox).where(
            ManagedSandbox.owner_type == "AGENT_WORKSPACE",
            ManagedSandbox.owner_id == workspace.id,
            ManagedSandbox.generation == runtime.active_generation,
            ManagedSandbox.desired_state == "RUNNING",
        )
    )
    if sandbox is None or not sandbox.backend_resource_name:
        return {"state": "UNAVAILABLE", "error_kind": "unknown", "checked_at": now().isoformat()}
    allocation = agent_workspace_host.runtime_allocation_for_agent_workspace(db, workspace.id)
    probe_id = str(uuid4())
    host_root = (
        Path(get_settings().workspace_root).resolve()
        / allocation.relative_root
        / "capabilities"
        / "mcp-readiness"
        / probe_id
    )
    runtime_root = Path("/runtime/capabilities/mcp-readiness") / probe_id
    capability = _frozen_runtime_capability(db, published, "MCP")
    try:
        _, _, servers = materialize_agent_workspace_capabilities(
            (capability,), host_root=host_root, runtime_root=runtime_root
        )
        result = get_runtime().probe_mcp(
            RuntimeMCPProbeRequest(
                server=servers[0],
                base_url=f"http://{sandbox.backend_resource_name}:8000",
                runtime_resource_name=sandbox.backend_resource_name,
            )
        )
    except DomainError:
        return {"state": "UNAVAILABLE", "error_kind": "unknown", "checked_at": now().isoformat()}
    finally:
        # The MCP test endpoint has consumed the materialized configuration
        # synchronously.  The temporary immutable probe directory is never a
        # Conversation capability and must not accumulate in the allocation.
        shutil.rmtree(host_root, ignore_errors=True)
    return {
        "state": "READY" if result.ok else "UNAVAILABLE",
        "error_kind": None if result.ok else (result.error_kind or "unknown"),
        "checked_at": now().isoformat(),
    }


def default_workspace(db: Session) -> dict[str, Any]:
    workspace = db.scalar(
        select(AgentWorkspace).where(AgentWorkspace.scope_key == "platform-default")
    )
    if workspace is None:
        raise DomainError("AGENT_WORKSPACE_NOT_READY", "Agent 工作区正在初始化", 503)
    return _workspace_dict(workspace)


def get_workspace(db: Session, workspace_id: str) -> dict[str, Any]:
    return _workspace_dict(_workspace(db, workspace_id))


def update_workspace_settings(
    db: Session, workspace_id: str, default_model_provider_id: str | None
) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    if default_model_provider_id is None:
        workspace.default_model_provider_id = None
    else:
        if not has_connected_default_model(db, default_model_provider_id):
            raise DomainError(
                "AGENT_MODEL_CONFIGURATION_INVALID",
                "默认模型必须是已测试成功且存在启用默认模型的配置",
                409,
            )
        workspace.default_model_provider_id = default_model_provider_id
    workspace.updated_at = now()
    db.flush()
    return _workspace_dict(workspace)


def runtime_status(db: Session, workspace_id: str) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    state = runtime.status if runtime is not None else "RECONNECTING"
    return {
        "state": "ACTIVE" if state == "ACTIVE" else "RECOVERING",
        "write_available": state == "ACTIVE" and workspace.desired_state == "RUNNING",
        "message": None if state == "ACTIVE" else "运行环境正在恢复，数据已保留",
        "updated_at": (
            runtime.updated_at if runtime is not None else workspace.updated_at
        ).isoformat(),
    }


def _handle(
    db: Session, workspace: AgentWorkspace, binding: AgentConversationBinding
) -> RuntimeHandle:
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None or runtime.status != "ACTIVE" or runtime.active_generation is None:
        code = (
            "AGENT_RUNTIME_DEGRADED"
            if runtime and runtime.status == "DEGRADED"
            else "AGENT_RUNTIME_RECOVERING"
        )
        raise DomainError(code, "Agent 运行环境正在恢复，数据已保留", 503)
    sandbox = db.scalar(
        select(ManagedSandbox).where(
            ManagedSandbox.owner_type == "AGENT_WORKSPACE",
            ManagedSandbox.owner_id == workspace.id,
            ManagedSandbox.generation == runtime.active_generation,
            ManagedSandbox.desired_state == "RUNNING",
        )
    )
    if sandbox is None:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
    return RuntimeHandle(
        job_id=f"agent-workspace:{workspace.id}",
        conversation_id=binding.openhands_conversation_id,
        runtime_resource_id=sandbox.id,
        runtime_resource_name=sandbox.backend_resource_name,
    )


def list_conversations(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    _workspace(db, workspace_id)
    return [
        _dict(db, item)
        for item in db.scalars(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
            .order_by(AgentConversationBinding.updated_at.desc())
        )
    ]


def get_conversation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    item = _binding(db, workspace_id, binding_id)
    item.last_connected_at = now()
    db.flush()
    return _dict(db, item)


def _capability_marketplace_paths(
    db: Session, workspace: AgentWorkspace, binding: AgentConversationBinding
) -> tuple[Path, Path, str]:
    allocation = agent_workspace_host.runtime_allocation_for_agent_workspace(db, workspace.id)
    host_root = (
        Path(get_settings().workspace_root).resolve()
        / allocation.relative_root
        / "capabilities"
        / "conversations"
        / binding.id
    )
    runtime_root = Path("/runtime/capabilities/conversations") / binding.id
    return host_root, runtime_root, agent_workspace_capability_marketplace_name(binding.id)


def _frozen_runtime_capability(db: Session, published: Any, capability_type: str) -> dict[str, Any]:
    runtime_config = published.runtime_config()
    return {
        "capability_version_id": published.version.id,
        "capability_type": capability_type,
        "capability_key": published.package.capability_key,
        "digest": published.version.digest,
        "normalized_config": dict(runtime_config),
        **runtime_config,
    }


def _freeze_capabilities(
    db: Session, binding: AgentConversationBinding, capability_version_ids: tuple[str, ...]
) -> None:
    for position, (published, capability_type) in enumerate(
        _validated_capabilities(
            db,
            capability_version_ids,
            allowed_types=_CREATION_CAPABILITY_TYPES,
        )
    ):
        db.add(
            AgentConversationCapability(
                binding_id=binding.id,
                capability_version_id=published.version.id,
                capability_type=capability_type,
                capability_key=published.package.capability_key,
                digest=published.version.digest,
                position=position,
            )
        )


def _freeze_workspace_capabilities(
    db: Session, workspace: AgentWorkspace, binding: AgentConversationBinding
) -> None:
    """Copy the current workspace selection into a native conversation manifest."""

    references = tuple(
        reference.capability_version_id
        for reference in db.scalars(
            select(AgentWorkspaceCapability)
            .where(AgentWorkspaceCapability.workspace_id == workspace.id)
            .order_by(AgentWorkspaceCapability.position)
        )
    )
    _freeze_capabilities(db, binding, references)


def _copy_frozen_capabilities(
    db: Session, source: AgentConversationBinding, target: AgentConversationBinding
) -> None:
    for reference in db.scalars(
        select(AgentConversationCapability)
        .where(AgentConversationCapability.binding_id == source.id)
        .order_by(AgentConversationCapability.position)
    ):
        db.add(
            AgentConversationCapability(
                binding_id=target.id,
                capability_version_id=reference.capability_version_id,
                capability_type=reference.capability_type,
                capability_key=reference.capability_key,
                digest=reference.digest,
                position=reference.position,
            )
        )


def _create_native_conversation(
    db: Session,
    workspace: AgentWorkspace,
    binding: AgentConversationBinding,
    provider: RuntimeProvider,
    working_directory: str,
    *,
    allow_existing: bool = False,
) -> RuntimeHandle:
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
    handle = _handle(db, workspace, binding)
    if allow_existing:
        try:
            identity = get_runtime().reload_conversation(handle)
        except DomainError as exc:
            if exc.status != 404:
                raise
        else:
            if identity.persistence_dir != (
                f"/runtime/state/conversations/{UUID(binding.openhands_conversation_id).hex}"
            ):
                raise DomainError(
                    "AGENT_CONVERSATION_IDENTITY_DRIFT", "会话持久化身份校验失败", 409
                )
            if identity.workspace_working_dir != working_directory:
                raise DomainError(
                    "AGENT_WORK_DIRECTORY_IDENTITY_DRIFT", "会话工作目录校验失败", 409
                )
            return handle
    host_root, runtime_root, _marketplace_name = _capability_marketplace_paths(
        db, workspace, binding
    )
    agent_spec = build_agent_spec(
        config_from_binding(db, binding),
        provider=provider,
        binding_id=binding.id,
        working_directory=working_directory,
        host_root=host_root,
        runtime_root=runtime_root,
    )
    request = StartAttemptRequest(
        attempt_id=binding.id,
        execution_key=f"agent-workspace:{workspace.id}:conversation:{binding.id}",
        node={"asset": {"name": "Agent Workspace"}},
        bindings=[],
        workspace_ref=working_directory,
        conversation_id=binding.openhands_conversation_id,
        agent_spec=agent_spec,
        environment_image=runtime.runtime_image_digest,
        environment_id=workspace.id,
        environment_version_id=workspace.id,
        environment_version_no=1,
        runtime_sandbox_id=handle.runtime_resource_id,
        runtime_resource_name=handle.runtime_resource_name,
        runtime_base_url=f"http://{handle.runtime_resource_name}:8000",
    )
    created = get_runtime().create_conversation(request)
    if created.conversation_id != binding.openhands_conversation_id:
        raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话身份校验失败", 409)
    identity = get_runtime().reload_conversation(
        replace(handle, conversation_id=binding.openhands_conversation_id)
    )
    if identity.persistence_dir != (
        f"/runtime/state/conversations/{UUID(binding.openhands_conversation_id).hex}"
    ):
        raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话持久化身份校验失败", 409)
    if identity.workspace_working_dir != working_directory:
        raise DomainError("AGENT_WORK_DIRECTORY_IDENTITY_DRIFT", "会话工作目录校验失败", 409)
    return replace(handle, conversation_id=binding.openhands_conversation_id)


def create_conversation(
    db: Session,
    workspace_id: str,
    title: str | None,
    model_provider_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    existing = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.create_idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.workspace_id != workspace.id:
            raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话创建请求冲突", 409)
        if existing.lifecycle == "ACTIVE":
            return _dict(db, existing)
        raise DomainError("AGENT_CONVERSATION_PROVISIONING", "会话仍在创建中", 409)
    if not model_provider_id or not has_connected_default_model(db, model_provider_id):
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "请选择已测试成功且存在启用默认模型的模型供应商",
            409,
        )
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
    provider = runtime_provider(
        db, {"asset": {"executor": {"model_provider_id": model_provider_id}}}
    )
    conversation_id = str(uuid4())
    binding = AgentConversationBinding(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        conversation_scope_id=workspace.id,
        runtime_session_id=runtime.id,
        model_provider_id=model_provider_id,
        model_name=provider.model,
        reasoning_effort=provider.reasoning_effort,
        streaming_callback_ready=True,
        openhands_conversation_id=conversation_id,
        working_directory=_PROJECT_ROOT,
        display_title=title.strip() if title and title.strip() else None,
        create_idempotency_key=idempotency_key,
    )
    db.add(binding)
    db.flush()
    # Keep the legacy programmatic creation path aligned with lazy bootstrap:
    # every native Agent conversation receives a durable copy of the current
    # workspace capability selection before its RuntimeAgentSpec is compiled.
    _freeze_workspace_capabilities(db, workspace, binding)
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        binding_id=binding.id,
        command_type="CREATE",
        idempotency_key=idempotency_key,
        attempt_count=1,
    )
    db.add(command)
    try:
        _create_native_conversation(db, workspace, binding, provider, _PROJECT_ROOT)
    except DomainError as exc:
        binding.lifecycle = "FAILED"
        command.state = "FAILED"
        command.last_error_code = exc.code
        command.failure_summary = "Conversation creation failed; inspect protected logs"
        raise
    binding.lifecycle = "ACTIVE"
    command.state = "SUCCEEDED"
    db.flush()
    return _dict(db, binding)


def add_conversation_capability(
    db: Session,
    workspace_id: str,
    binding_id: str,
    capability_version_id: str,
) -> dict[str, Any]:
    """Dynamically add one governed capability through native Plugin loading.

    The catalog reference is written only after OpenHands acknowledges the
    formal ``load_plugin`` request.  This prevents a control-plane checkbox
    from claiming a Skill/MCP/Plugin is active when the native runtime rejected
    it.
    """

    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace.id, binding_id, lock=True)
    if binding.lifecycle != "ACTIVE" or not binding.working_directory:
        raise DomainError("AGENT_CONVERSATION_NOT_READY", "会话当前无法加载能力", 409)
    existing = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id,
            AgentConversationCapability.capability_version_id == capability_version_id,
        )
    )
    if existing is not None:
        return _dict(db, binding)
    selected = _validated_capabilities(db, (capability_version_id,))
    published, capability_type = selected[0]
    conflict = db.scalar(
        select(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding.id,
            AgentConversationCapability.capability_type == capability_type,
            AgentConversationCapability.capability_key == published.package.capability_key,
        )
    )
    if conflict is not None:
        raise DomainError(
            "AGENT_CONVERSATION_CAPABILITY_CONFLICT",
            "同类型同名称能力已在此会话加载其他版本",
            409,
            {"capability_key": published.package.capability_key},
        )
    handle = _handle(db, workspace, binding)
    if not get_runtime().can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_RUNNING", "会话运行中，完成当前回复后再加载能力", 409)
    host_root, runtime_root, marketplace_name = _capability_marketplace_paths(
        db, workspace, binding
    )
    plugin_name = materialize_agent_workspace_capability_marketplace(
        _frozen_runtime_capability(db, published, capability_type),
        host_root=host_root,
        runtime_root=runtime_root,
        marketplace_name=marketplace_name,
    )
    if plugin_name is None:
        raise DomainError("RUNTIME_CAPABILITY_UNAVAILABLE", "能力插件物化失败", 409)
    # This is the formal OpenHands conversation lifecycle endpoint.  Existing
    # pre-registration conversations fail here rather than receiving a fake
    # FlowWeave-only capability record.
    get_runtime().load_plugin(handle, f"{plugin_name}@{marketplace_name}")
    position = db.scalar(
        select(AgentConversationCapability.position)
        .where(AgentConversationCapability.binding_id == binding.id)
        .order_by(AgentConversationCapability.position.desc())
        .limit(1)
    )
    db.add(
        AgentConversationCapability(
            binding_id=binding.id,
            capability_version_id=published.version.id,
            capability_type=capability_type,
            capability_key=published.package.capability_key,
            digest=published.version.digest,
            position=(position + 1) if position is not None else 0,
        )
    )
    binding.updated_at = now()
    db.flush()
    return _dict(db, binding)


def _initial_user_event_id(handle: RuntimeHandle, previous_event_id: str | None) -> str | None:
    """Find a first user event exclusively through formal native identities."""

    expected_parent_id = previous_event_id or "__root__"
    candidates = [
        event.cursor
        for event in get_runtime().read_active_events(handle).events
        if event.event_type == "MESSAGE"
        and str(event.payload.get("source") or "").lower() in {"user", "human"}
        and event.payload.get("parent_id") == expected_parent_id
    ]
    return candidates[0] if len(candidates) == 1 else None


def _bootstrap_result(db: Session, binding: AgentConversationBinding) -> dict[str, Any]:
    return {
        "conversation": _dict(db, binding),
        "accepted": True,
        "cursor": binding.initial_user_event_id,
    }


def normalized_first_sentence(content: str) -> str:
    """A useful local title while the independent metadata task is pending."""

    first_line = next((line for line in content.splitlines() if line.strip()), "")
    normalized = " ".join(first_line.split())[:80]
    if _MECHANICAL_TITLE.fullmatch(normalized):
        return f"关于“{normalized}”的请求"[:80]
    return normalized or "用户请求"


def _enqueue_title_task(db: Session, binding: AgentConversationBinding, first_message: str) -> None:
    if not binding.model_provider_id or not binding.model_name:
        return
    enqueue(
        db,
        task_type="GENERATE_AGENT_CONVERSATION_TITLE",
        aggregate_type="AGENT_CONVERSATION",
        aggregate_id=binding.id,
        idempotency_key=(
            f"generate-agent-conversation-title:{binding.id}:{binding.title_generation}"
        ),
        payload={
            "title_generation": binding.title_generation,
            "model_provider_id": binding.model_provider_id,
            "model_name": binding.model_name,
            # This transient seed is redacted by the handler after its single
            # use. It is not an Agent Conversation/event projection.
            "first_message": " ".join(first_message.split())[:4000],
            "fallback_title": binding.display_title,
        },
    )


def _activate_bootstrapped_conversation(
    db: Session,
    binding: AgentConversationBinding,
    command: AgentConversationCommand,
    initial_event_id: str,
    first_message: str,
    attachments: tuple[dict[str, str | int], ...] = (),
) -> dict[str, Any]:
    binding.initial_user_event_id = initial_event_id
    binding.display_title = normalized_first_sentence(first_message)
    binding.title_state = "PENDING"
    binding.lifecycle = "ACTIVE"
    binding.updated_at = now()
    command.state = "SUCCEEDED"
    command.updated_at = binding.updated_at
    _record_message_attachments(db, binding, initial_event_id, first_message, attachments)
    _enqueue_title_task(db, binding, first_message)
    db.commit()
    return _bootstrap_result(db, binding)


def _bootstrap_command(
    db: Session, workspace_id: str, idempotency_key: str
) -> tuple[AgentConversationBinding | None, AgentConversationCommand | None]:
    binding = db.scalar(
        select(AgentConversationBinding)
        .where(AgentConversationBinding.create_idempotency_key == idempotency_key)
        .with_for_update()
    )
    if binding is None:
        return None, None
    if binding.workspace_id != workspace_id:
        raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话创建请求冲突", 409)
    command = db.scalar(
        select(AgentConversationCommand)
        .where(
            AgentConversationCommand.binding_id == binding.id,
            AgentConversationCommand.command_type == "CREATE",
        )
        .with_for_update()
    )
    if command is None:
        raise DomainError("AGENT_CONVERSATION_BOOTSTRAP_INVALID", "会话创建命令数据不完整", 409)
    return binding, command


def _record_bootstrap_failure(
    db: Session,
    binding: AgentConversationBinding,
    command: AgentConversationCommand,
    error: DomainError,
) -> None:
    """Discard a definitively failed lazy bootstrap reservation.

    Only ambiguous external outcomes retain reconciliation state. A known
    failure must leave no Binding or command row behind.
    """

    del error
    # Draft attachments are already private to this reserved binding ID.  A
    # definitively failed first message must release those files too, even
    # though no attachment projection has been committed yet.
    if binding.workspace_id is not None:
        agent_workspace_host.delete_session_attachment_files(db, binding.workspace_id, binding.id)
    db.delete(command)
    db.flush()
    db.delete(binding)
    db.commit()


def bootstrap_conversation(
    db: Session,
    workspace_id: str,
    *,
    work_directory_id: str | None,
    conversation_id: str | None = None,
    model_provider_id: str | None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    content: str,
    attachments: tuple[dict[str, str | int], ...] = (),
    capability_version_ids: tuple[str, ...] = (),
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a native conversation only while accepting its first user event.

    A browser draft has no database representation. The first submission first
    commits a private reservation keyed by the browser command ID, then creates
    the original native UUID and submits exactly one user event. An ambiguous
    submit is reconciled by native event IDs and parent IDs, never resent.
    """

    message_text = content.strip()
    if not message_text and not attachments:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    prompt, image_urls = _message_payload(message_text, attachments)
    workspace = _workspace(db, workspace_id)
    binding, command = _bootstrap_command(db, workspace.id, idempotency_key)
    if binding is not None and command is not None:
        # A deleted conversation deliberately retains its command record as an
        # audit tombstone.  A browser can still hold the old draft UUID after
        # deletion, but it must not treat that tombstone as an in-progress
        # bootstrap or reuse its idempotency key.
        if binding.lifecycle == "DELETED":
            raise DomainError(
                "AGENT_BOOTSTRAP_DRAFT_EXPIRED",
                "原会话已删除，请重新发送首条消息",
                409,
            )
        if binding.lifecycle == "ACTIVE" and binding.initial_user_event_id is not None:
            return _bootstrap_result(db, binding)
        if command.state == "AMBIGUOUS":
            reconciled = _initial_user_event_id(
                _handle(db, workspace, binding),
                previous_event_id=binding.bootstrap_parent_event_id,
            )
            if reconciled is not None:
                return _activate_bootstrapped_conversation(
                    db, binding, command, reconciled, message_text, attachments
                )
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息正在安全对账，请稍后重试；系统不会重复发送",
                504,
                {"binding_id": binding.id},
            )
        if command.state != "PENDING" or binding.lifecycle != "PROVISIONING":
            raise DomainError("AGENT_CONVERSATION_PROVISIONING", "会话创建仍在处理中", 409)
        command.attempt_count += 1
        db.commit()
    else:
        if not model_provider_id or not has_connected_default_model(db, model_provider_id):
            raise DomainError(
                "AGENT_MODEL_CONFIGURATION_REQUIRED",
                "请选择已测试成功且存在启用默认模型的模型供应商",
                409,
            )
        version_id, working_directory = agent_workspace_host.conversation_work_directory_context(
            db, workspace.id, work_directory_id
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        if runtime is None:
            raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
        provider = runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": model_provider_id}}},
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        try:
            binding_id = str(UUID(conversation_id)) if conversation_id else str(uuid4())
        except ValueError as exc:
            raise DomainError("AGENT_CONVERSATION_ID_INVALID", "会话标识无效", 422) from exc
        binding = AgentConversationBinding(
            id=binding_id,
            workspace_id=workspace.id,
            host_kind="AGENT_WORKSPACE",
            host_id=workspace.id,
            conversation_scope_id=workspace.id,
            runtime_session_id=runtime.id,
            work_directory_version_id=version_id,
            working_directory=working_directory,
            model_provider_id=provider.provider_id,
            model_name=provider.model,
            reasoning_effort=provider.reasoning_effort,
            streaming_callback_ready=True,
            openhands_conversation_id=str(uuid4()),
            create_idempotency_key=idempotency_key,
        )
        db.add(binding)
        db.flush()
        _freeze_capabilities(db, binding, capability_version_ids)
        command = AgentConversationCommand(
            workspace_id=workspace.id,
            host_kind="AGENT_WORKSPACE",
            host_id=workspace.id,
            binding_id=binding.id,
            command_type="CREATE",
            idempotency_key=idempotency_key,
            attempt_count=1,
        )
        db.add(command)
        # This durable reservation serializes retries before an external
        # OpenHands call and intentionally remains invisible to conversation
        # lists until the first user MessageEvent is accepted.
        db.commit()

    assert binding is not None and command is not None
    _validate_attachment_owners(binding.id, attachments)
    if not binding.model_provider_id or not binding.working_directory:
        error = DomainError("AGENT_CONVERSATION_BOOTSTRAP_INVALID", "会话创建数据不完整", 409)
        _record_bootstrap_failure(db, binding, command, error)
        raise error
    if binding.work_directory_version_id is not None:
        frozen_working_directory = agent_workspace_host.frozen_conversation_work_directory_context(
            db, workspace.id, binding.work_directory_version_id
        )
        if frozen_working_directory != binding.working_directory:
            error = DomainError(
                "AGENT_WORK_DIRECTORY_IDENTITY_DRIFT", "会话冻结工作目录校验失败", 409
            )
            _record_bootstrap_failure(db, binding, command, error)
            raise error
    provider = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": binding.model_provider_id}}},
        model_name=binding.model_name,
        reasoning_effort=binding.reasoning_effort,
    )
    try:
        handle = _create_native_conversation(
            db,
            workspace,
            binding,
            provider,
            binding.working_directory,
            allow_existing=command.attempt_count > 1,
        )
        previous_event_id = get_runtime().reload_conversation(handle).event_id
        binding.bootstrap_parent_event_id = previous_event_id
        db.commit()
    except DomainError as exc:
        if exc.code == "MCP_INITIALIZATION_UNAVAILABLE":
            error = _mcp_readiness_error(
                str(exc.details.get("error_kind") or "unknown"),
                _binding_mcp_keys(db, binding),
            )
            _record_bootstrap_failure(db, binding, command, error)
            raise error from exc
        if exc.details.get("outcome_unknown") is True:
            command.last_error_code = exc.code
            command.failure_summary = "Conversation creation requires retry with the original UUID"
            db.commit()
            raise DomainError(
                "AGENT_BOOTSTRAP_CREATION_AMBIGUOUS",
                "会话创建结果不确定，请使用同一请求标识重试",
                504,
            ) from exc
        _record_bootstrap_failure(db, binding, command, exc)
        raise
    try:
        delivered = get_runtime().send_message(handle, prompt, image_urls)
    except DomainError as exc:
        if exc.details.get("outcome_unknown") is True:
            try:
                reconciled = _initial_user_event_id(handle, previous_event_id)
            except DomainError:
                reconciled = None
            if reconciled is not None:
                return _activate_bootstrapped_conversation(
                    db, binding, command, reconciled, message_text, attachments
                )
            command.state = "AMBIGUOUS"
            command.last_error_code = exc.code
            command.failure_summary = (
                "First user event delivery requires native identity reconciliation"
            )
            db.commit()
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息正在安全对账，请稍后重试；系统不会重复发送",
                504,
                {"binding_id": binding.id},
            ) from exc
        try:
            get_runtime().delete_conversation(handle)
        except DomainError:
            pass
        _record_bootstrap_failure(db, binding, command, exc)
        raise
    initial_event_id = delivered.cursor or _initial_user_event_id(handle, previous_event_id)
    if initial_event_id is None:
        command.state = "AMBIGUOUS"
        command.failure_summary = "First user event ID was unavailable after accepted delivery"
        db.commit()
        raise DomainError(
            "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
            "首条消息正在安全对账，请稍后重试；系统不会重复发送",
            504,
            {"binding_id": binding.id},
        )
    return _activate_bootstrapped_conversation(
        db, binding, command, initial_event_id, message_text, attachments
    )


def patch_conversation(
    db: Session, workspace_id: str, binding_id: str, title: str
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise DomainError("AGENT_CONVERSATION_TITLE_REQUIRED", "会话标题不能为空", 422)
    workspace = _workspace(db, workspace_id)
    item = _binding(db, workspace_id, binding_id, lock=True)
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        binding_id=item.id,
        command_type="RENAME",
        idempotency_key=f"rename-agent-conversation:{item.id}:{uuid4()}",
        attempt_count=1,
    )
    db.add(command)
    try:
        get_runtime().rename_conversation(_handle(db, workspace, item), clean_title)
    except DomainError as exc:
        command.state = "FAILED"
        command.last_error_code = exc.code
        command.failure_summary = "Conversation rename failed; inspect protected logs"
        raise
    item.display_title = clean_title
    item.title_state = "MANUAL"
    item.title_generation += 1
    item.updated_at = now()
    command.state = "SUCCEEDED"
    db.flush()
    return _dict(db, item)


def delete_conversation(
    db: Session, workspace_id: str, binding_id: str, idempotency_key: str
) -> None:
    workspace = _workspace(db, workspace_id)
    item = _binding(db, workspace_id, binding_id, lock=True)
    existing = db.scalar(
        select(AgentConversationCommand).where(
            AgentConversationCommand.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.workspace_id != workspace.id or existing.binding_id != item.id:
            raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话删除请求冲突", 409)
        if existing.state == "SUCCEEDED":
            return
        raise DomainError("AGENT_CONVERSATION_DELETE_PENDING", "会话删除仍在处理中", 409)
    item.lifecycle = "DELETE_PENDING"
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        binding_id=item.id,
        command_type="DELETE",
        idempotency_key=idempotency_key,
        attempt_count=1,
    )
    db.add(command)
    try:
        get_runtime().delete_conversation(_handle(db, workspace, item))
    except DomainError as exc:
        command.state = "FAILED"
        command.last_error_code = exc.code
        command.failure_summary = "Conversation deletion failed; inspect protected logs"
        item.lifecycle = "ACTIVE"
        raise
    # The native conversation is now gone, so its private attachment objects
    # must not outlive it.  The helper only unlinks files bearing this binding's
    # opaque owner UUID and never traverses arbitrary workspace paths.
    agent_workspace_host.delete_session_attachment_files(db, workspace.id, item.id)
    item.lifecycle = "DELETED"
    item.deleted_at = now()
    command.state = "SUCCEEDED"
    db.flush()


def events(db: Session, workspace_id: str, binding_id: str, cursor: str | None) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace_id, binding_id)
    handle = _handle(db, workspace, binding)
    batch = get_runtime().read_active_events(replace(handle, cursor=cursor))
    # OpenHands 1.44 persists a Condensation when compaction finishes, but an
    # automatic event/token-triggered compaction has no separate durable start
    # event and the Condensation itself does not retain its trigger reason.
    # Enrich the browser projection from the formal active branch so the UI can
    # render an auditable start record and a separate completion record without
    # changing native conversation history.
    try:
        context = get_runtime().conversation_context(handle)
    except (DomainError, AttributeError):
        context = {}
    raw_max_events = context.get("condenser_max_size")
    max_events = (
        raw_max_events
        if isinstance(raw_max_events, int)
        and not isinstance(raw_max_events, bool)
        and raw_max_events > 0
        else None
    )
    events_by_id = {event.cursor: event for event in batch.events}
    condensation_metadata: dict[str, dict[str, Any]] = {}
    for event in batch.events:
        if event.event_type != "CONDENSATION_COMPLETED":
            continue
        # Associate a request and an earlier condensation only through the
        # formal active-branch parent chain. REST ordering is transport detail
        # and must never decide conversation causality.
        ancestors: list[Any] = []
        seen_ids: set[str] = set()
        parent_id = event.payload.get("parent_id")
        while isinstance(parent_id, str) and parent_id != "__root__":
            if parent_id in seen_ids:
                break
            seen_ids.add(parent_id)
            ancestor = events_by_id.get(parent_id)
            if ancestor is None:
                break
            ancestors.append(ancestor)
            parent_id = ancestor.payload.get("parent_id")
        previous_completion = next(
            (item for item in ancestors if item.event_type == "CONDENSATION_COMPLETED"),
            None,
        )
        current_ancestors = (
            ancestors[: ancestors.index(previous_completion)]
            if previous_completion is not None
            else ancestors
        )
        pending_request = next(
            (item for item in current_ancestors if item.event_type == "CONDENSATION_REQUESTED"),
            None,
        )
        # A first condensation's complete formal ancestry provides a lower
        # bound for the native View size. After an earlier condensation, the
        # summary/retained-tail View cannot be reconstructed from event order,
        # so leave the detailed automatic reason unspecified.
        event_count = len(current_ancestors)
        if pending_request is not None:
            reason = "REQUEST"
            reason_detail = (
                "OpenHands 收到显式压缩请求；该请求可能来自手动压缩、"
                "上下文用量主动保护或模型上下文超限后的恢复。"
            )
            trigger_event = pending_request
        elif previous_completion is None and max_events is not None and event_count > max_events:
            reason = "EVENTS"
            reason_detail = (
                f"压缩前正式事件链至少有 {event_count} 个事件，"
                f"已超过该会话的 {max_events} 条事件上限。"
            )
            trigger_event = current_ancestors[0] if current_ancestors else event
        else:
            reason = "AUTOMATIC_CONTEXT_PROTECTION"
            reason_detail = (
                "OpenHands 自动上下文保护已触发；原生完成事件未保存更细的触发分类，"
                "因此无法可靠区分 token 压力与模型恢复。"
            )
            trigger_event = current_ancestors[0] if current_ancestors else event
        condensation_metadata[event.cursor] = {
            "condensation_reason": reason,
            "condensation_reason_detail": reason_detail,
            "condensation_triggered_at": trigger_event.payload.get("timestamp"),
            "condensation_completed_at": event.payload.get("timestamp"),
            "condensation_event_count": event_count,
            "condensation_max_events": max_events,
            "condensation_request_event_id": (
                pending_request.cursor if pending_request is not None else None
            ),
        }
    event_ids = [event.cursor for event in batch.events if event.event_type == "MESSAGE"]
    stored: list[AgentConversationMessageAttachment] = (
        list(
            db.scalars(
                select(AgentConversationMessageAttachment).where(
                    AgentConversationMessageAttachment.binding_id == binding.id,
                    AgentConversationMessageAttachment.event_id.in_(event_ids),
                )
            ).all()
        )
        if event_ids
        else []
    )
    attachments_by_event: dict[str, list[AgentConversationMessageAttachment]] = {}
    for attachment in stored:
        attachments_by_event.setdefault(attachment.event_id, []).append(attachment)

    def projected_event(event: Any) -> dict[str, Any]:
        payload = dict(event.payload)
        if event.event_type == "CONDENSATION_COMPLETED":
            payload.update(condensation_metadata.get(event.cursor, {}))
        content = payload.get("content")
        if isinstance(content, str):
            payload["content"] = _project_sandbox_images(
                content, workspace_id=workspace.id, binding_id=binding.id
            )
        attachments = attachments_by_event.get(event.cursor, [])
        if attachments:
            payload["display_content"] = attachments[0].content
            payload["attachments"] = [
                {
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "byte_size": attachment.byte_size,
                    "path": attachment.path,
                }
                for attachment in attachments
            ]
        elif event.event_type == "MESSAGE" and str(payload.get("source") or "").lower() in {
            "user",
            "human",
        }:
            # Before attachment metadata was projected, OpenHands persisted a
            # product-generated path suffix in the native message body.  Keep
            # that old history readable without teaching the browser to parse
            # private runtime paths.  The strict upload-path validator ensures
            # only paths issued by this product are converted.
            display_content, legacy_paths = _legacy_message_attachments(
                str(payload.get("content") or "")
            )
            if legacy_paths:
                payload["display_content"] = display_content
                payload["attachments"] = [
                    {
                        "filename": _attachment_filename(path),
                        "mime_type": "application/octet-stream",
                        "byte_size": 0,
                        "path": path,
                    }
                    for path in legacy_paths
                ]
        return {"id": event.cursor, "event_type": event.event_type, "payload": payload}

    return {
        "events": [projected_event(event) for event in batch.events],
        "next_cursor": batch.cursor,
    }


def pending_confirmation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    pending = get_runtime().get_pending_confirmation(
        _handle(db, workspace, _binding(db, workspace_id, binding_id))
    )
    if pending is None:
        return {"pending": False}
    return {
        "pending": True,
        "pending_actions_digest": pending.pending_actions_digest,
        "cursor": pending.cursor,
        "actions": [
            {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
                "arguments": action.arguments,
                "security_risk": action.security_risk,
                "summary": action.summary,
                "digest": action.digest,
            }
            for action in pending.actions
        ],
    }


def decide_confirmation(
    db: Session,
    workspace_id: str,
    binding_id: str,
    *,
    expected_pending_digest: str,
    accept: bool,
    reason: str,
) -> dict[str, Any]:
    clean_reason = reason.strip()
    if not clean_reason:
        raise DomainError("AGENT_CONFIRMATION_REASON_REQUIRED", "请填写确认理由", 422)
    workspace = _workspace(db, workspace_id)
    result = get_runtime().respond_to_confirmation(
        _handle(db, workspace, _binding(db, workspace_id, binding_id, lock=True)),
        expected_pending_digest,
        accept,
        clean_reason,
    )
    return {"accepted": True, "cursor": result.cursor}


def runtime_stream_details(
    db: Session, workspace_id: str, binding_id: str
) -> tuple[str | None, RuntimeHandle]:
    workspace = _workspace(db, workspace_id)
    return get_settings().runtime_adapter, _handle(
        db, workspace, _binding(db, workspace_id, binding_id)
    )


def _terminal_sandbox(db: Session, workspace_id: str) -> ManagedSandbox:
    workspace = _workspace(db, workspace_id)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None or runtime.status != "ACTIVE" or runtime.active_generation is None:
        raise DomainError("AGENT_TERMINAL_UNAVAILABLE", "Agent 运行环境正在恢复，无法连接终端", 503)
    sandbox = db.scalar(
        select(ManagedSandbox).where(
            ManagedSandbox.owner_type == "AGENT_WORKSPACE",
            ManagedSandbox.owner_id == workspace.id,
            ManagedSandbox.generation == runtime.active_generation,
            ManagedSandbox.desired_state == "RUNNING",
        )
    )
    if sandbox is None:
        raise DomainError("AGENT_TERMINAL_UNAVAILABLE", "Agent 运行环境正在恢复，无法连接终端", 503)
    if not sandbox.backend_resource_id:
        raise DomainError("AGENT_TERMINAL_UNAVAILABLE", "Agent 运行环境正在恢复，无法连接终端", 503)
    return sandbox


def terminal_resource_details(db: Session, workspace_id: str) -> tuple[str, str]:
    sandbox = _terminal_sandbox(db, workspace_id)
    return sandbox.backend_resource_name, sandbox.id


def terminal_container_details(db: Session, workspace_id: str) -> tuple[str, str, str]:
    """Return the owned Runtime locator and its diagnostic container identity."""

    sandbox = _terminal_sandbox(db, workspace_id)
    return sandbox.backend_resource_name, sandbox.id, sandbox.backend_resource_id


def _uses_legacy_compaction_policy(context: dict[str, Any]) -> bool:
    raw_max_events = context.get("condenser_max_size")
    return (
        isinstance(raw_max_events, int)
        and not isinstance(raw_max_events, bool)
        and raw_max_events < _AGENT_WORKSPACE_CONDENSER_MAX_EVENTS
    )


def _compaction_summary_is_structured(summary: object) -> bool:
    if not isinstance(summary, str) or not summary.strip():
        return False
    # Strip Markdown emphasis markers without changing the protocol field
    # names themselves (notably the underscore in USER_CONTEXT).
    normalized = re.sub(r"[*`]", "", summary.upper())
    # These are the stable handoff anchors required by OpenHands' own fixed
    # summarizing prompt. CURRENT_STATE/TASK_TRACKING are task-dependent, but
    # the user goal and pending work must always survive a safe checkpoint.
    return all(
        re.search(
            rf"(?:^|\n)[ \t]*(?:#+[ \t]*)?{section}[ \t]*:[ \t]*\S",
            normalized,
        )
        for section in ("USER_CONTEXT", "COMPLETED", "PENDING")
    )


def _rollback_unsafe_compaction(runtime: Any, handle: RuntimeHandle, event_id: str) -> None:
    try:
        runtime.navigate(handle, event_id)
    except DomainError as exc:
        raise DomainError(
            "AGENT_CONTEXT_COMPACTION_ROLLBACK_FAILED",
            "压缩验收失败且无法恢复压缩前 HEAD；会话已停止，消息尚未发送",
            503,
            {"event_id": event_id},
        ) from exc


def _safe_native_compaction(runtime: Any, handle: RuntimeHandle) -> str:
    before_identity = runtime.reload_conversation(handle)
    before_event_id = before_identity.event_id
    if not before_event_id:
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "压缩前会话缺少正式 HEAD 身份", 409)
    before_events = runtime.read_active_events(handle).events
    before_ids = {event.cursor for event in before_events}
    completed_before = {
        event.cursor for event in before_events if event.event_type == "CONDENSATION_COMPLETED"
    }
    request_error: DomainError | None = None
    try:
        runtime.condense(handle)
    except DomainError as exc:
        # Only a transport interruption has an unknown outcome. An HTTP error
        # is a definitive Agent Server rejection even when its status is 5xx;
        # waiting for a completion event in that case misreports the failure as
        # a lost response and leaves the UI spinning for the reconciliation
        # window. Never retry either mutation.
        if exc.details.get("outcome_unknown") is not True:
            _rollback_unsafe_compaction(runtime, handle, before_event_id)
            raise DomainError(
                "AGENT_CONTEXT_COMPACTION_FAILED",
                (
                    "OpenHands condenser 明确返回失败；摘要模型或当前事件结构"
                    "无法生成有效压缩。已恢复压缩前 HEAD，消息尚未发送"
                ),
                502 if exc.status >= 500 else exc.status,
                {"upstream_code": exc.code},
            ) from exc
        # The synchronous request may have completed before its response was
        # lost. Reconcile only this explicit unknown-outcome class against the
        # durable native request/completion ancestry.
        request_error = exc

    deadline = time.monotonic() + _COMPACTION_EVENT_WAIT_SECONDS
    completed_event: Any | None = None
    while time.monotonic() < deadline:
        after_events = runtime.read_active_events(handle).events
        by_id = {event.cursor: event for event in after_events}
        new_requests = [
            event
            for event in after_events
            if event.event_type == "CONDENSATION_REQUESTED" and event.cursor not in before_ids
        ]
        new_completions = [
            event
            for event in after_events
            if event.event_type == "CONDENSATION_COMPLETED" and event.cursor not in completed_before
        ]
        new_request_ids = {event.cursor for event in new_requests}
        for candidate in reversed(new_completions):
            explicit_request_id = candidate.payload.get("condensation_request_event_id")
            if isinstance(explicit_request_id, str) and explicit_request_id in new_request_ids:
                completed_event = candidate
                break
            ancestor_id = candidate.payload.get("parent_id")
            seen: set[str] = set()
            while isinstance(ancestor_id, str) and ancestor_id != "__root__":
                if ancestor_id in seen:
                    break
                seen.add(ancestor_id)
                ancestor = by_id.get(ancestor_id)
                if ancestor is None:
                    break
                if (
                    ancestor.event_type == "CONDENSATION_REQUESTED"
                    and ancestor.cursor not in before_ids
                ):
                    completed_event = candidate
                    break
                ancestor_id = ancestor.payload.get("parent_id")
            if completed_event is not None:
                break
        # OpenHands 1.40's formal Condensation event does not guarantee either
        # parent_id or condensation_request_event_id.  The binding is locked
        # and can_accept_input was checked before this mutation, so one unique
        # request/completion pair added after the before-snapshot is an
        # unambiguous durable acknowledgement even when the optional ancestry
        # fields are absent. Never accept a completion without its new request.
        if completed_event is None and len(new_requests) == len(new_completions) == 1:
            completed_event = new_completions[0]
        if completed_event is not None:
            break
        time.sleep(0.1)
    if completed_event is None:
        _rollback_unsafe_compaction(runtime, handle, before_event_id)
        raise DomainError(
            (
                "AGENT_CONTEXT_COMPACTION_DELIVERY_AMBIGUOUS"
                if request_error is not None
                else "AGENT_CONTEXT_COMPACTION_INCOMPLETE"
            ),
            (
                "OpenHands 压缩响应丢失且未发现可关联的完成事件；"
                if request_error is not None
                else "OpenHands 未持久化可关联本次请求的压缩完成事件；"
            )
            + "已恢复压缩前 HEAD，消息尚未发送",
            503,
        ) from request_error
    forgotten = {str(value) for value in completed_event.payload.get("forgotten_event_ids", [])}
    user_events = [
        event
        for event in before_events
        if event.event_type == "MESSAGE"
        and str(event.payload.get("source") or "").lower() in {"user", "human"}
    ]
    protected_user_ids = {event.cursor for event in user_events[:1] + user_events[-1:]}
    if protected_user_ids & forgotten:
        _rollback_unsafe_compaction(runtime, handle, before_event_id)
        raise DomainError(
            "AGENT_CONTEXT_COMPACTION_UNSAFE",
            "压缩移除了用户的初始目标或最近纠偏；已恢复压缩前 HEAD，消息尚未发送",
            409,
            {"condensation_event_id": completed_event.cursor},
        )
    if not _compaction_summary_is_structured(completed_event.payload.get("summary")):
        _rollback_unsafe_compaction(runtime, handle, before_event_id)
        raise DomainError(
            "AGENT_CONTEXT_COMPACTION_UNSAFE",
            "压缩摘要没有完整保留用户目标、已完成事项和待办；已恢复压缩前 HEAD，消息尚未发送",
            409,
            {"condensation_event_id": completed_event.cursor},
        )

    return completed_event.cursor


def message(
    db: Session,
    workspace_id: str,
    binding_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...] = (),
) -> dict[str, Any]:
    if not content.strip() and not attachments:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace_id, binding_id, lock=True)
    _validate_attachment_owners(binding.id, attachments)
    prompt, image_urls = _message_payload(content, attachments)
    if not binding.streaming_callback_ready:
        raise DomainError(
            "AGENT_STREAMING_MIGRATION_REQUIRED",
            "此历史会话需要先迁移到流式会话后才能继续发送",
            409,
            {"binding_id": binding.id},
        )
    handle = _handle(db, workspace, binding)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError(
            "AGENT_CONVERSATION_BUSY",
            "Agent 正在处理上一条消息或停止请求，请稍候",
            409,
        )
    # OpenHands switch_llm changes the live Event Service but does not persist
    # the replacement LLM. Re-apply the complete persisted binding before
    # every user event so a Runtime reload cannot silently restore the model
    # used when this Conversation was originally created.
    target_provider_id = binding.model_provider_id
    if not target_provider_id or not has_connected_default_model(db, target_provider_id):
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "请选择已测试成功且存在启用默认模型的模型供应商",
            409,
        )
    recovery = runtime.incomplete_fork_recovery(handle)
    if recovery is not None:
        source_handle = replace(
            handle,
            conversation_id=recovery.source_conversation_id,
            cursor=recovery.source_leaf_event_id,
        )
        replacement_id = str(
            uuid5(
                UUID(binding.id),
                f"finish-boundary-recovery:{handle.conversation_id}:{recovery.completed_event_id}",
            )
        )
        recovery_provider = runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": binding.model_provider_id}}},
            model_name=binding.model_name,
            reasoning_effort=binding.reasoning_effort,
        )
        repaired = runtime.fork_conversation(
            source_handle,
            target_conversation_id=replacement_id,
            title=binding.display_title or "未命名会话",
            from_event_id=recovery.completed_event_id,
            expected_source_leaf_event_id=recovery.source_leaf_event_id,
            reset_metrics=True,
            condenser=RuntimeCondenser(
                kind="LLM_SUMMARIZING",
                max_size=_AGENT_WORKSPACE_CONDENSER_MAX_EVENTS,
                max_tokens_ratio=_PROACTIVE_COMPACTION_RATIO,
                keep_first=4,
            ),
            condenser_provider=recovery_provider,
        )
        if repaired.leaf_event_id != recovery.completed_event_id:
            raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "异常分叉会话恢复边界校验失败", 409)
        handle = repaired.handle
        if not runtime.can_accept_input(handle):
            raise DomainError("RUNTIME_FORK_NOT_WRITABLE", "异常分叉会话恢复后仍不可继续输入", 503)
        binding.openhands_conversation_id = replacement_id
        binding.updated_at = now()
        db.flush()
    provider = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": target_provider_id}}},
        model_name=binding.model_name,
        reasoning_effort=binding.reasoning_effort,
    )
    try:
        runtime.switch_model(handle, provider)
    except DomainError as exc:
        raise DomainError(
            "AGENT_MODEL_REBIND_FAILED",
            "无法在发送前应用会话已保存的模型，请稍后重试",
            503,
            {"model_provider_id": target_provider_id},
        ) from exc
    compacted = False
    context = runtime.conversation_context(handle)
    # OpenHands' public fork API deep-copies the source agent and has no field
    # for replacing its condenser.  A fork of a historical 240-event session
    # must therefore remain writable without silently trusting that old
    # automatic summary boundary.  Force the same verified native compaction
    # used by the current token policy before every new turn.  The verifier
    # checks durable request/completion ancestry and rolls HEAD back on an
    # unsafe summary, so the user event is never sent on failed protection.
    if _uses_legacy_compaction_policy(context) or _proactive_compaction_required(
        runtime, handle, context
    ):
        _safe_native_compaction(runtime, handle)
        compacted = True
    try:
        result = runtime.send_message(handle, prompt, image_urls)
    except DomainError as exc:
        if exc.status >= 500:
            raise DomainError(
                "AGENT_MESSAGE_DELIVERY_AMBIGUOUS", "消息发送结果不确定，请先刷新会话", 504
            ) from exc
        raise
    if result.cursor:
        _record_message_attachments(db, binding, result.cursor, content.strip(), attachments)
    return {"accepted": True, "cursor": result.cursor, "compacted": compacted}


_ATTACHMENT_PATH = re.compile(
    r"^/runtime/workspace/project/uploads/"
    r"(?P<owner>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<object>[0-9a-f]{32})(?:--(?P<filename>[A-Za-z0-9][A-Za-z0-9._-]{0,180}))?$"
)
_LEGACY_ATTACHMENT_PATH = re.compile(
    r"^/runtime/workspace/project/uploads/[0-9a-f]{32}-[A-Za-z0-9._-]{1,180}$"
)
_ATTACHMENT_SUFFIX = re.compile(
    r"(?:\n\n)?(?:已上传到共享工作区的附件：|请查看已上传到共享工作区的附件：)\n"
    r"(?P<paths>(?:- /runtime/workspace/project/uploads/.+\n?)+)$"
)


def _attachment_filename(path: str) -> str:
    """Extract the originally uploaded filename from a validated upload path."""

    filename = path.rsplit("/", 1)[-1]
    matched = _ATTACHMENT_PATH.fullmatch(path)
    if matched:
        return matched.group("filename") or "attachment"
    return filename[33:]


def _legacy_message_attachments(content: str) -> tuple[str, tuple[str, ...]]:
    """Project the pre-metadata attachment suffix without changing native history."""

    matched = _ATTACHMENT_SUFFIX.search(content)
    if matched is None:
        return content, ()
    paths = tuple(
        line.removeprefix("- ")
        for line in matched.group("paths").splitlines()
        if _ATTACHMENT_PATH.fullmatch(line.removeprefix("- "))
        or _LEGACY_ATTACHMENT_PATH.fullmatch(line.removeprefix("- "))
    )
    if not paths:
        return content, ()
    return content[: matched.start()].strip(), paths


def _message_payload(
    content: str, attachments: tuple[dict[str, str | int], ...]
) -> tuple[str, tuple[str, ...]]:
    if len(attachments) > 10:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件引用无效，请重新上传", 422)
    paths: list[str] = []
    image_urls: list[str] = []
    for item in attachments:
        path = item.get("path")
        image_data_url = item.get("image_data_url")
        if (
            not isinstance(path, str)
            or _ATTACHMENT_PATH.fullmatch(path) is None
            or (
                image_data_url is not None
                and (
                    not isinstance(image_data_url, str)
                    or not image_data_url.startswith("data:image/")
                )
            )
        ):
            raise DomainError("AGENT_ATTACHMENT_INVALID", "附件引用无效，请重新上传", 422)
        paths.append(path)
        if isinstance(image_data_url, str):
            image_urls.append(image_data_url)
    prompt = content.strip()
    if paths:
        prompt += (
            "\n\n已上传到共享工作区的附件：\n" if prompt else "请查看已上传到共享工作区的附件：\n"
        ) + "\n".join(f"- {path}" for path in paths)
    return prompt, tuple(image_urls)


def _validate_attachment_owners(
    binding_id: str, attachments: tuple[dict[str, str | int], ...]
) -> None:
    """Reject private attachment paths belonging to another conversation."""

    for item in attachments:
        matched = _ATTACHMENT_PATH.fullmatch(str(item.get("path") or ""))
        if matched is None or matched.group("owner") != binding_id:
            raise DomainError("AGENT_ATTACHMENT_INVALID", "附件不属于当前会话，请重新上传", 422)


def _record_message_attachments(
    db: Session,
    binding: AgentConversationBinding,
    event_id: str,
    content: str,
    attachments: tuple[dict[str, str | int], ...],
) -> None:
    """Persist safe display metadata against the native user event identity."""

    for item in attachments:
        path = str(item["path"])
        filename = str(item.get("filename") or _attachment_filename(path))
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        byte_size = item.get("byte_size")
        db.add(
            AgentConversationMessageAttachment(
                binding_id=binding.id,
                event_id=event_id,
                content=content,
                filename=filename[:240],
                mime_type=mime_type[:200],
                byte_size=byte_size if isinstance(byte_size, int) and byte_size >= 0 else 0,
                path=path,
            )
        )
    if attachments:
        db.flush()


def upload_attachment(
    db: Session,
    workspace_id: str,
    binding_id: str | None,
    *,
    filename: str,
    content_type: str,
    content: bytes,
    work_directory_id: str | None = None,
    attachment_owner_id: str | None = None,
) -> dict[str, str | int | None]:
    if not filename or len(filename) > 240 or "\x00" in filename:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件文件名无效", 422)
    if not content or len(content) > 25 * 1024 * 1024:
        raise DomainError("AGENT_ATTACHMENT_TOO_LARGE", "单个附件不能超过 25 MiB", 422)
    mime_type = content_type.lower().strip() or "application/octet-stream"
    if len(mime_type) > 200:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件类型无效", 422)
    workspace = _workspace(db, workspace_id)
    bound_conversation: AgentConversationBinding | None = None
    if binding_id is None:
        try:
            owner_id = str(UUID(attachment_owner_id or ""))
        except ValueError as exc:
            raise DomainError("AGENT_CONVERSATION_ID_INVALID", "附件必须关联有效会话", 422) from exc
        agent_workspace_host.conversation_work_directory_context(
            db, workspace.id, work_directory_id
        )
        resource_name, resource_id = terminal_resource_details(db, workspace.id)
        handle = RuntimeHandle(
            job_id=f"agent-workspace:{workspace.id}",
            conversation_id="",
            runtime_resource_id=resource_id,
            runtime_resource_name=resource_name,
        )
    else:
        bound_conversation = _binding(db, workspace_id, binding_id)
        owner_id = bound_conversation.id
        handle = _handle(db, workspace, bound_conversation)
    path = get_runtime().upload_workspace_file(
        handle,
        filename=filename,
        content_type=mime_type,
        content=content,
        attachment_owner_id=owner_id,
    )
    matched_path = _ATTACHMENT_PATH.fullmatch(path)
    if matched_path is None or matched_path.group("owner") != owner_id:
        raise DomainError("RUNTIME_PROTOCOL_ERROR", "OpenHands 返回了无效附件路径", 502)
    # A conversation-bound upload is safe to preview before the user sends it.
    # Persist a pending projection so the file endpoint can authorize this exact
    # opaque upload path without exposing the shared uploads directory.  A
    # formal MessageEvent later receives its own projection for transcript UI.
    if bound_conversation is not None:
        db.add(
            AgentConversationMessageAttachment(
                binding_id=bound_conversation.id,
                event_id=f"pending:{uuid4()}",
                content="",
                filename=filename,
                mime_type=mime_type,
                byte_size=len(content),
                path=path,
            )
        )
        db.flush()
    image_data_url = (
        f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        if mime_type.startswith("image/")
        else None
    )
    return {
        "filename": filename,
        "mime_type": mime_type,
        "byte_size": len(content),
        "path": path,
        "image_data_url": image_data_url,
    }


def _context_usage_is_current(runtime: Any, handle: RuntimeHandle) -> bool:
    """Whether per_turn_token was produced after the latest condensation.

    OpenHands keeps the last main-LLM usage snapshot when only its independent
    condenser LLM has run. Treat that value as stale until a subsequent formal
    agent/model event exists; otherwise the UI would keep showing the
    pre-compaction percentage as if it described the compacted View.
    """

    events = runtime.read_active_events(handle).events
    latest_condensation = max(
        (
            index
            for index, event in enumerate(events)
            if event.event_type == "CONDENSATION_COMPLETED"
        ),
        default=-1,
    )
    if latest_condensation < 0:
        return True
    for event in events[latest_condensation + 1 :]:
        if event.event_type in {"THOUGHT", "TOOL_CALL", "COMPLETED"}:
            return True
        if event.event_type == "MESSAGE" and str(event.payload.get("source") or "").lower() not in {
            "user",
            "human",
        }:
            return True
    return False


def _proactive_compaction_required(
    runtime: Any, handle: RuntimeHandle, context: dict[str, Any]
) -> bool:
    """Use only OpenHands' registered current-View usage and window."""

    used_tokens = context.get("used_tokens")
    window_tokens = context.get("window_tokens")
    return (
        _context_usage_is_current(runtime, handle)
        and isinstance(used_tokens, int)
        and not isinstance(used_tokens, bool)
        and isinstance(window_tokens, int)
        and not isinstance(window_tokens, bool)
        and window_tokens > 0
        and used_tokens >= int(window_tokens * _PROACTIVE_COMPACTION_RATIO)
    )


def conversation_context(
    db: Session, workspace_id: str, binding_id: str
) -> dict[str, int | float | str | bool | None]:
    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace_id, binding_id)
    runtime = get_runtime()
    handle = _handle(db, workspace, binding)
    context = runtime.conversation_context(handle)
    usage_current = _context_usage_is_current(runtime, handle)
    window_tokens = context.get("window_tokens")
    threshold_tokens = (
        int(window_tokens * _PROACTIVE_COMPACTION_RATIO)
        if isinstance(window_tokens, int)
        and not isinstance(window_tokens, bool)
        and window_tokens > 0
        else None
    )
    condenser_max_size = context.get("condenser_max_size")
    compaction_policy_current = not (
        isinstance(condenser_max_size, int)
        and not isinstance(condenser_max_size, bool)
        and condenser_max_size < _AGENT_WORKSPACE_CONDENSER_MAX_EVENTS
    )
    return {
        **context,
        "used_tokens": context.get("used_tokens") if usage_current else None,
        "usage_current": usage_current,
        "proactive_compaction_ratio": _PROACTIVE_COMPACTION_RATIO,
        "proactive_compaction_tokens": threshold_tokens,
        "compaction_policy_current": compaction_policy_current,
    }


def switch_conversation_model(
    db: Session,
    workspace_id: str,
    binding_id: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
) -> dict[str, str | None]:
    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace_id, binding_id, lock=True)
    if not has_connected_default_model(db, model_provider_id):
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "请选择已测试成功且存在启用默认模型的模型供应商",
            409,
        )
    handle = _handle(db, workspace, binding)
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后切换模型", 409)
    provider = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": model_provider_id}}},
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    # Historical Event Services without a streaming callback retain the
    # selection now, then apply it through their mandatory native fork before
    # the next user event. New conversations can switch immediately.
    if binding.streaming_callback_ready:
        runtime.switch_model(handle, provider)
    binding.model_provider_id = provider.provider_id
    binding.model_name = provider.model
    binding.reasoning_effort = provider.reasoning_effort
    binding.updated_at = now()
    db.flush()
    return {
        "model_provider_id": provider.provider_id,
        "model_name": provider.model,
        "reasoning_effort": provider.reasoning_effort,
    }


def condense_conversation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id, lock=True))
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后压缩上下文", 409)
    cursor = _safe_native_compaction(runtime, handle)
    return {"accepted": True, "cursor": cursor}


def _fork_conversation(
    db: Session,
    workspace_id: str,
    binding_id: str,
    event_id: str | None,
    title: str | None,
    idempotency_key: str,
    *,
    migration_provider_id: str | None = None,
    migration_model_name: str | None = None,
    migration_reasoning_effort: str | None = None,
) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    source = _binding(db, workspace_id, binding_id, lock=True)
    existing = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.create_idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.workspace_id != workspace.id:
            raise DomainError("AGENT_CONVERSATION_COMMAND_CONFLICT", "会话分叉请求冲突", 409)
        if existing.lifecycle == "ACTIVE":
            return _dict(db, existing)
        raise DomainError("AGENT_CONVERSATION_PROVISIONING", "分叉会话仍在创建中", 409)
    if migration_provider_id is not None and source.streaming_callback_ready:
        raise DomainError(
            "AGENT_STREAMING_MIGRATION_NOT_REQUIRED",
            "当前会话已经支持流式模型切换",
            409,
        )
    runtime = get_runtime()
    source_handle = _handle(db, workspace, source)
    if not runtime.can_accept_input(source_handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后分叉会话", 409)
    source_identity = runtime.reload_conversation(source_handle)
    if not source_identity.event_id:
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "当前会话缺少可分叉的事件身份", 409)
    target_provider_id = source.model_provider_id
    target_model_name = source.model_name
    target_reasoning_effort = source.reasoning_effort
    streaming_callback_ready = source.streaming_callback_ready
    if migration_provider_id is not None:
        if not has_connected_default_model(db, migration_provider_id):
            raise DomainError(
                "AGENT_MODEL_CONFIGURATION_REQUIRED",
                "请选择已测试成功且存在启用默认模型的模型供应商",
                409,
            )
        provider: RuntimeProvider = runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": migration_provider_id}}},
            model_name=migration_model_name,
            reasoning_effort=migration_reasoning_effort,
        )
        try:
            runtime.switch_model(source_handle, provider)
        except DomainError as exc:
            raise DomainError(
                "AGENT_STREAMING_MIGRATION_FAILED",
                "无法为历史会话准备流式模型，请稍后重试",
                503,
                {"model_provider_id": migration_provider_id},
            ) from exc
        target_provider_id = provider.provider_id
        target_model_name = provider.model
        target_reasoning_effort = provider.reasoning_effort
        streaming_callback_ready = True
        event_id = source_identity.event_id
    if event_id is None:
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "当前会话缺少可分叉的事件身份", 409)
    fork_event_id = runtime.resolve_fork_boundary(source_handle, event_id)
    # A FinishObservation may be persisted while resolve_fork_boundary waits
    # for the selected FinishAction's execution closure.  Refresh the native
    # source HEAD after resolution so the fork CAS accepts that legitimate
    # terminal advance while still rejecting any later drift.
    source_identity = runtime.reload_conversation(source_handle)
    if not source_identity.event_id:
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "当前会话缺少可分叉的事件身份", 409)
    if migration_provider_id is not None:
        display_title = source.display_title
        runtime_title = source.display_title or "未命名会话"
    else:
        runtime_title = (title or "").strip()[:240] or f"Fork · {source.display_title or '会话'}"
        display_title = runtime_title
    target_id = str(uuid4())
    target = AgentConversationBinding(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        conversation_scope_id=workspace.id,
        runtime_session_id=source.runtime_session_id,
        model_provider_id=target_provider_id,
        model_name=target_model_name,
        reasoning_effort=target_reasoning_effort,
        streaming_callback_ready=streaming_callback_ready,
        openhands_conversation_id=target_id,
        display_title=display_title,
        create_idempotency_key=idempotency_key,
    )
    db.add(target)
    db.flush()
    _copy_frozen_capabilities(db, source, target)
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace.id,
        binding_id=target.id,
        command_type="FORK",
        idempotency_key=idempotency_key,
        attempt_count=1,
    )
    db.add(command)
    try:
        if not target_provider_id:
            raise DomainError(
                "AGENT_MODEL_CONFIGURATION_REQUIRED",
                "分叉会话缺少可用的模型供应商",
                409,
            )
        fork_provider = runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": target_provider_id}}},
            model_name=target_model_name,
            reasoning_effort=target_reasoning_effort,
        )
        result = runtime.fork_conversation(
            source_handle,
            target_conversation_id=target_id,
            title=runtime_title,
            from_event_id=fork_event_id,
            expected_source_leaf_event_id=source_identity.event_id,
            reset_metrics=True,
            condenser=RuntimeCondenser(
                kind="LLM_SUMMARIZING",
                max_size=_AGENT_WORKSPACE_CONDENSER_MAX_EVENTS,
                max_tokens_ratio=_PROACTIVE_COMPACTION_RATIO,
                keep_first=4,
            ),
            condenser_provider=fork_provider,
        )
        if (
            result.handle.conversation_id != target_id
            or result.source_conversation_id != source.openhands_conversation_id
            or result.source_event_id != fork_event_id
        ):
            raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "会话分叉身份校验失败", 409)
        identity = runtime.reload_conversation(result.handle)
        if identity.persistence_dir != f"/runtime/state/conversations/{UUID(target_id).hex}":
            raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "分叉会话持久化身份校验失败", 409)
        if identity.event_id != fork_event_id or not runtime.can_accept_input(result.handle):
            raise DomainError(
                "RUNTIME_FORK_NOT_WRITABLE", "分叉会话未停在可继续输入的完整回复边界", 503
            )
        fork_context = runtime.conversation_context(result.handle)
        if fork_context.get("condenser_max_size") != _AGENT_WORKSPACE_CONDENSER_MAX_EVENTS:
            raise DomainError(
                "RUNTIME_FORK_POLICY_DRIFT",
                "分叉会话上下文压缩策略校验失败",
                409,
            )
        if migration_provider_id is not None:
            active_provider_id = fork_context.get("provider_id")
            if active_provider_id != target_provider_id:
                raise DomainError(
                    "RUNTIME_FORK_IDENTITY_DRIFT",
                    "流式迁移后的模型供应商身份校验失败",
                    409,
                )
    except DomainError as exc:
        target.lifecycle = "FAILED"
        command.state = "FAILED"
        command.last_error_code = exc.code
        command.failure_summary = "Conversation fork failed; inspect protected logs"
        raise
    target.lifecycle = "ACTIVE"
    command.state = "SUCCEEDED"
    db.flush()
    return _dict(db, target)


def fork_conversation(
    db: Session,
    workspace_id: str,
    binding_id: str,
    event_id: str,
    title: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    return _fork_conversation(db, workspace_id, binding_id, event_id, title, idempotency_key)


def migrate_streaming_conversation(
    db: Session,
    workspace_id: str,
    binding_id: str,
    model_provider_id: str,
    model_name: str | None,
    reasoning_effort: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    return _fork_conversation(
        db,
        workspace_id,
        binding_id,
        None,
        None,
        idempotency_key,
        migration_provider_id=model_provider_id,
        migration_model_name=model_name,
        migration_reasoning_effort=reasoning_effort,
    )


def interrupt(db: Session, workspace_id: str, binding_id: str) -> None:
    workspace = _workspace(db, workspace_id)
    get_runtime().interrupt(
        _handle(db, workspace, _binding(db, workspace_id, binding_id, lock=True))
    )


def input_readiness(db: Session, workspace_id: str, binding_id: str) -> dict[str, bool]:
    workspace = _workspace(db, workspace_id)
    ready = get_runtime().can_accept_input(
        _handle(db, workspace, _binding(db, workspace_id, binding_id))
    )
    return {"ready": ready}


def rewrite_message(
    db: Session, workspace_id: str, binding_id: str, event_id: str, content: str
) -> dict[str, Any]:
    if not content.strip():
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    workspace = _workspace(db, workspace_id)
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id, lock=True))
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请先暂停当前回复", 409)
    legacy_policy = _uses_legacy_compaction_policy(runtime.conversation_context(handle))
    batch = runtime.read_active_events(handle)
    user_events = [
        event
        for event in batch.events
        if event.event_type == "MESSAGE"
        and str(event.payload.get("source") or "").lower() in {"user", "human"}
    ]
    target = next((event for event in user_events if event.cursor == event_id), None)
    if target is None or not user_events or user_events[-1].cursor != event_id:
        raise DomainError(
            "AGENT_MESSAGE_REWRITE_UNAVAILABLE",
            "只能编辑当前活动分支中最近发送的消息",
            409,
        )
    parent_id = target.payload.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        raise DomainError("RUNTIME_EVENT_IDENTITY_INVALID", "消息事件身份无效", 409)
    runtime.navigate(handle, parent_id)
    # Editing the first user message leaves no inherited context to protect.
    # Other legacy branches are compacted only after navigation so the native
    # summary is built from the branch the replacement turn will actually use.
    if legacy_policy and parent_id not in {None, "__root__"}:
        _safe_native_compaction(runtime, handle)
    try:
        result = runtime.send_message(handle, content.strip())
    except DomainError as exc:
        if exc.status >= 500:
            raise DomainError(
                "AGENT_MESSAGE_DELIVERY_AMBIGUOUS", "重新发送结果不确定，请先刷新会话", 504
            ) from exc
        raise
    return {"accepted": True, "cursor": result.cursor}


def resume(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    runtime = get_runtime()
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id))
    if _uses_legacy_compaction_policy(runtime.conversation_context(handle)):
        _safe_native_compaction(runtime, handle)
    result = runtime.run(handle)
    return {"accepted": True, "cursor": result.cursor}


# Shared FlowRun-node conversations use these helpers while retaining one
# implementation for native Agent Workspace conversations.  Public aliases
# keep that dependency explicit without exposing underscore-prefixed details.
PROACTIVE_COMPACTION_RATIO = _PROACTIVE_COMPACTION_RATIO
AGENT_WORKSPACE_CONDENSER_MAX_EVENTS = _AGENT_WORKSPACE_CONDENSER_MAX_EVENTS
ATTACHMENT_PATH = _ATTACHMENT_PATH
enqueue_title_task = _enqueue_title_task
frozen_runtime_capability = _frozen_runtime_capability
initial_user_event_id = _initial_user_event_id
message_payload = _message_payload
record_message_attachments = _record_message_attachments
validate_attachment_owners = _validate_attachment_owners
