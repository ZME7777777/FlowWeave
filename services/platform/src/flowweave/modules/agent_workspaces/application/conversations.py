from __future__ import annotations

import base64
import re
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCommand,
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.modules.model_providers.public import has_connected_default_model
from flowweave.modules.sandboxes.public import ManagedSandbox
from flowweave.runtime.base import (
    RuntimeAgentSpec,
    RuntimeCondenser,
    RuntimeHandle,
    RuntimeTool,
    StartAttemptRequest,
)
from flowweave.runtime.contract import agent_workspace_runtime_contract
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.request import runtime_provider
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.settings import get_settings

_TOOLS = (
    RuntimeTool(name="terminal"),
    RuntimeTool(name="file_editor"),
    RuntimeTool(name="task_tracker"),
)


def _workspace(db: Session, workspace_id: str) -> AgentWorkspace:
    item = db.get(AgentWorkspace, workspace_id)
    if item is None:
        raise not_found("agent_workspace", workspace_id)
    return item


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


def _dict(item: AgentConversationBinding) -> dict[str, Any]:
    return {
        "id": item.id,
        "display_title": item.display_title,
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
        _dict(item)
        for item in db.scalars(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.workspace_id == workspace_id,
                AgentConversationBinding.lifecycle != "DELETED",
            )
            .order_by(AgentConversationBinding.updated_at.desc())
        )
    ]


def get_conversation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    item = _binding(db, workspace_id, binding_id)
    item.last_connected_at = now()
    db.flush()
    return _dict(item)


def create_conversation(
    db: Session, workspace_id: str, title: str | None, idempotency_key: str
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
            return _dict(existing)
        raise DomainError("AGENT_CONVERSATION_PROVISIONING", "会话仍在创建中", 409)
    if not workspace.default_model_provider_id:
        raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "请先选择已测试成功的模型配置", 409)
    if not has_connected_default_model(db, workspace.default_model_provider_id):
        raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "请先选择已测试成功的模型配置", 409)
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    if runtime is None:
        raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
    conversation_id = str(uuid4())
    binding = AgentConversationBinding(
        workspace_id=workspace.id,
        runtime_session_id=runtime.id,
        openhands_conversation_id=conversation_id,
        display_title=title.strip() if title and title.strip() else None,
        create_idempotency_key=idempotency_key,
    )
    db.add(binding)
    db.flush()
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        binding_id=binding.id,
        command_type="CREATE",
        idempotency_key=idempotency_key,
        attempt_count=1,
    )
    db.add(command)
    provider = runtime_provider(
        db, {"asset": {"executor": {"model_provider_id": workspace.default_model_provider_id}}}
    )
    handle = _handle(db, workspace, binding)
    request = StartAttemptRequest(
        attempt_id=binding.id,
        execution_key=f"agent-workspace:{workspace.id}:conversation:{binding.id}",
        node={"asset": {"name": "Agent Workspace"}},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id=conversation_id,
        agent_spec=RuntimeAgentSpec(
            provider=provider,
            # This is the fixed OpenHands 1.42.0 summarizing condenser, not a
            # FlowWeave summary loop.  It emits CondensationRequest/Condensation
            # events that remain in the native conversation history.
            condenser=RuntimeCondenser(kind="LLM_SUMMARIZING"),
            condenser_provider=provider,
            tools=_TOOLS,
            runtime_contract=agent_workspace_runtime_contract(tuple(tool.name for tool in _TOOLS)),
        ),
        environment_image=runtime.runtime_image_digest,
        environment_id=workspace.id,
        environment_version_id=workspace.id,
        environment_version_no=1,
        runtime_sandbox_id=handle.runtime_resource_id,
        runtime_resource_name=handle.runtime_resource_name,
        runtime_base_url=f"http://{handle.runtime_resource_name}:8000",
    )
    try:
        created = get_runtime().create_conversation(request)
        if created.conversation_id != conversation_id:
            raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话身份校验失败", 409)
        identity = get_runtime().reload_conversation(
            replace(handle, conversation_id=conversation_id)
        )
        if identity.persistence_dir != f"/runtime/state/conversations/{UUID(conversation_id).hex}":
            raise DomainError("AGENT_CONVERSATION_IDENTITY_DRIFT", "会话持久化身份校验失败", 409)
    except DomainError as exc:
        binding.lifecycle = "FAILED"
        command.state = "FAILED"
        command.last_error_code = exc.code
        command.failure_summary = "Conversation creation failed; inspect protected logs"
        raise
    binding.lifecycle = "ACTIVE"
    command.state = "SUCCEEDED"
    db.flush()
    return _dict(binding)


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
    item.updated_at = now()
    command.state = "SUCCEEDED"
    db.flush()
    return _dict(item)


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
    item.lifecycle = "DELETED"
    item.deleted_at = now()
    command.state = "SUCCEEDED"
    db.flush()


def events(db: Session, workspace_id: str, binding_id: str, cursor: str | None) -> dict[str, Any]:
    workspace = _workspace(db, workspace_id)
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id))
    batch = get_runtime().read_active_events(replace(handle, cursor=cursor))
    return {
        "events": [
            {"id": event.cursor, "event_type": event.event_type, "payload": event.payload}
            for event in batch.events
        ],
        "next_cursor": batch.cursor,
    }


def runtime_stream_details(
    db: Session, workspace_id: str, binding_id: str
) -> tuple[str | None, RuntimeHandle]:
    workspace = _workspace(db, workspace_id)
    return get_settings().runtime_adapter, _handle(
        db, workspace, _binding(db, workspace_id, binding_id)
    )


def terminal_resource_details(db: Session, workspace_id: str) -> tuple[str, str]:
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
    return sandbox.backend_resource_name, sandbox.id


def message(
    db: Session,
    workspace_id: str,
    binding_id: str,
    content: str,
    attachments: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    if not content.strip():
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    if len(attachments) > 10 or any(
        _ATTACHMENT_PATH.fullmatch(item.get("path", "")) is None
        or ("image_data_url" in item and not item["image_data_url"].startswith("data:image/"))
        for item in attachments
    ):
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件引用无效，请重新上传", 422)
    workspace = _workspace(db, workspace_id)
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id, lock=True))
    if not get_runtime().can_accept_input(handle):
        raise DomainError(
            "AGENT_CONVERSATION_BUSY",
            "Agent 正在处理上一条消息或停止请求，请稍候",
            409,
        )
    try:
        paths = tuple(item["path"] for item in attachments)
        image_urls = tuple(
            item["image_data_url"] for item in attachments if "image_data_url" in item
        )
        prompt = content.strip()
        if paths:
            prompt += "\n\n已上传到共享工作区的附件：\n" + "\n".join(f"- {path}" for path in paths)
        result = get_runtime().send_message(handle, prompt, image_urls)
    except DomainError as exc:
        if exc.status >= 500:
            raise DomainError(
                "AGENT_MESSAGE_DELIVERY_AMBIGUOUS", "消息发送结果不确定，请先刷新会话", 504
            ) from exc
        raise
    return {"accepted": True, "cursor": result.cursor}


_ATTACHMENT_PATH = re.compile(
    r"^/runtime/workspace/project/uploads/[0-9a-f]{32}-[A-Za-z0-9._-]{1,180}$"
)


def upload_attachment(
    db: Session,
    workspace_id: str,
    binding_id: str,
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> dict[str, str | int | None]:
    if not filename or len(filename) > 240 or "\x00" in filename:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件文件名无效", 422)
    if not content or len(content) > 25 * 1024 * 1024:
        raise DomainError("AGENT_ATTACHMENT_TOO_LARGE", "单个附件不能超过 25 MiB", 422)
    mime_type = content_type.lower().strip() or "application/octet-stream"
    if len(mime_type) > 200:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件类型无效", 422)
    workspace = _workspace(db, workspace_id)
    path = get_runtime().upload_workspace_file(
        _handle(db, workspace, _binding(db, workspace_id, binding_id)),
        filename=filename,
        content_type=mime_type,
        content=content,
    )
    if _ATTACHMENT_PATH.fullmatch(path) is None:
        raise DomainError("RUNTIME_PROTOCOL_ERROR", "OpenHands 返回了无效附件路径", 502)
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


def conversation_context(
    db: Session, workspace_id: str, binding_id: str
) -> dict[str, int | str | None]:
    workspace = _workspace(db, workspace_id)
    binding = _binding(db, workspace_id, binding_id)
    return get_runtime().conversation_context(_handle(db, workspace, binding))


def switch_conversation_model(
    db: Session,
    workspace_id: str,
    binding_id: str,
    provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
) -> dict[str, str | None]:
    workspace = _workspace(db, workspace_id)
    handle = _handle(db, workspace, _binding(db, workspace_id, binding_id))
    runtime = get_runtime()
    if not runtime.can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后切换模型", 409)
    provider = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": provider_id}}},
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    runtime.switch_model(handle, provider)
    return {
        "model_provider_id": provider.provider_id,
        "model_name": provider.model,
        "reasoning_effort": provider.reasoning_effort,
    }


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
    result = get_runtime().run(_handle(db, workspace, _binding(db, workspace_id, binding_id)))
    return {"accepted": True, "cursor": result.cursor}
