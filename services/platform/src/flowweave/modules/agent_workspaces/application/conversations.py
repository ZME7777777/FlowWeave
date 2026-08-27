from __future__ import annotations

import base64
import re
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCommand,
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.modules.model_providers.public import has_connected_default_model
from flowweave.modules.sandboxes.public import ManagedSandbox
from flowweave.modules.tasks.application.service import enqueue
from flowweave.runtime.base import (
    RuntimeAgentContext,
    RuntimeAgentSpec,
    RuntimeCondenser,
    RuntimeHandle,
    RuntimeProvider,
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

_PROJECT_ROOT = "/runtime/workspace/project"
_PROJECT_ROOT_SYSTEM_CONTEXT = "\n".join(
    (
        "当前会话的项目根目录是 /runtime/workspace/project。",
        "所有需要保留的代码、配置、文档和用户产物必须写入该目录或其子目录。",
        "可按需求或功能自行创建子目录；优先使用相对于项目根的路径。",
        "不要将用户项目文件写入项目根以外的位置，例如 /runtime 的其他目录、/tmp 或 HOME。",
        "不要向用户解释宿主机路径、Docker 挂载或容器实现细节；对用户而言，这就是项目根目录。",
    )
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
        "title_state": item.title_state,
        "model_provider_id": item.model_provider_id,
        "model_name": item.model_name,
        "reasoning_effort": item.reasoning_effort,
        "work_directory_version_id": item.work_directory_version_id,
        "working_directory": item.working_directory,
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
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
            .order_by(AgentConversationBinding.updated_at.desc())
        )
    ]


def get_conversation(db: Session, workspace_id: str, binding_id: str) -> dict[str, Any]:
    item = _binding(db, workspace_id, binding_id)
    item.last_connected_at = now()
    db.flush()
    return _dict(item)


def _system_context(working_directory: str) -> str:
    if working_directory == _PROJECT_ROOT:
        return _PROJECT_ROOT_SYSTEM_CONTEXT
    return _PROJECT_ROOT_SYSTEM_CONTEXT + (
        f"\n本次会话的默认工作目录是 {working_directory}；"
        "优先在该目录及其子目录内组织本次工作的文件。"
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
    request = StartAttemptRequest(
        attempt_id=binding.id,
        execution_key=f"agent-workspace:{workspace.id}:conversation:{binding.id}",
        node={"asset": {"name": "Agent Workspace"}},
        bindings=[],
        workspace_ref=working_directory,
        conversation_id=binding.openhands_conversation_id,
        agent_spec=RuntimeAgentSpec(
            provider=provider,
            confirmation_policy="NEVER",
            agent_context=RuntimeAgentContext(
                system_message_suffix=_system_context(working_directory)
            ),
            # This is the fixed OpenHands 1.42.0 summarizing condenser, not a
            # FlowWeave summary loop. It remains native Conversation history.
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
            return _dict(existing)
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
    command = AgentConversationCommand(
        workspace_id=workspace.id,
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
    return _dict(binding)


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


def _bootstrap_result(binding: AgentConversationBinding) -> dict[str, Any]:
    return {
        "conversation": _dict(binding),
        "accepted": True,
        "cursor": binding.initial_user_event_id,
    }


def normalized_first_sentence(content: str) -> str:
    """A useful local title while the independent metadata task is pending."""

    first_line = next((line for line in content.splitlines() if line.strip()), "")
    normalized = " ".join(first_line.split())
    return normalized[:80] or "新会话"


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
) -> dict[str, Any]:
    binding.initial_user_event_id = initial_event_id
    binding.display_title = normalized_first_sentence(first_message)
    binding.title_state = "PENDING"
    binding.lifecycle = "ACTIVE"
    binding.updated_at = now()
    command.state = "SUCCEEDED"
    command.updated_at = binding.updated_at
    _enqueue_title_task(db, binding, first_message)
    db.commit()
    return _bootstrap_result(binding)


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
    binding.lifecycle = "FAILED"
    command.state = "FAILED"
    command.last_error_code = error.code
    command.failure_summary = "Conversation bootstrap failed; inspect protected logs"
    db.commit()


def bootstrap_conversation(
    db: Session,
    workspace_id: str,
    *,
    work_directory_id: str | None,
    model_provider_id: str | None,
    content: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a native conversation only while accepting its first user event.

    A browser draft has no database representation. The first submission first
    commits a private reservation keyed by the browser command ID, then creates
    the original native UUID and submits exactly one user event. An ambiguous
    submit is reconciled by native event IDs and parent IDs, never resent.
    """

    prompt = content.strip()
    if not prompt:
        raise DomainError("AGENT_MESSAGE_EMPTY", "消息不能为空", 422)
    workspace = _workspace(db, workspace_id)
    binding, command = _bootstrap_command(db, workspace.id, idempotency_key)
    if binding is not None and command is not None:
        if binding.lifecycle == "ACTIVE" and binding.initial_user_event_id is not None:
            return _bootstrap_result(binding)
        if command.state == "AMBIGUOUS":
            reconciled = _initial_user_event_id(
                _handle(db, workspace, binding),
                previous_event_id=binding.bootstrap_parent_event_id,
            )
            if reconciled is not None:
                return _activate_bootstrapped_conversation(db, binding, command, reconciled, prompt)
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息发送结果仍不确定，请稍后刷新后继续对账",
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
        version_id, working_directory = work_directories.conversation_context(
            db, workspace.id, work_directory_id
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        if runtime is None:
            raise DomainError("AGENT_RUNTIME_RECOVERING", "Agent 运行环境正在恢复，数据已保留", 503)
        provider = runtime_provider(
            db, {"asset": {"executor": {"model_provider_id": model_provider_id}}}
        )
        binding = AgentConversationBinding(
            workspace_id=workspace.id,
            runtime_session_id=runtime.id,
            work_directory_version_id=version_id,
            working_directory=working_directory,
            model_provider_id=model_provider_id,
            model_name=provider.model,
            reasoning_effort=provider.reasoning_effort,
            streaming_callback_ready=True,
            openhands_conversation_id=str(uuid4()),
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
        # This durable reservation serializes retries before an external
        # OpenHands call and intentionally remains invisible to conversation
        # lists until the first user MessageEvent is accepted.
        db.commit()

    assert binding is not None and command is not None
    if not binding.model_provider_id or not binding.working_directory:
        error = DomainError("AGENT_CONVERSATION_BOOTSTRAP_INVALID", "会话创建数据不完整", 409)
        _record_bootstrap_failure(db, binding, command, error)
        raise error
    if binding.work_directory_version_id is not None:
        frozen_working_directory = work_directories.frozen_conversation_context(
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
        if exc.status >= 500:
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
        delivered = get_runtime().send_message(handle, prompt)
    except DomainError as exc:
        if exc.status >= 500:
            try:
                reconciled = _initial_user_event_id(handle, previous_event_id)
            except DomainError:
                reconciled = None
            if reconciled is not None:
                return _activate_bootstrapped_conversation(db, binding, command, reconciled, prompt)
            command.state = "AMBIGUOUS"
            command.last_error_code = exc.code
            command.failure_summary = (
                "First user event delivery requires native identity reconciliation"
            )
            db.commit()
            raise DomainError(
                "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS",
                "首条消息发送结果不确定，请稍后刷新后继续对账",
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
            "首条消息发送结果不确定，请稍后刷新后继续对账",
            504,
            {"binding_id": binding.id},
        )
    return _activate_bootstrapped_conversation(db, binding, command, initial_event_id, prompt)


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
    item.title_state = "MANUAL"
    item.title_generation += 1
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
    binding = _binding(db, workspace_id, binding_id, lock=True)
    if not binding.streaming_callback_ready:
        raise DomainError(
            "AGENT_STREAMING_MIGRATION_REQUIRED",
            "此历史会话需要先迁移到流式会话后才能继续发送",
            409,
            {"binding_id": binding.id},
        )
    handle = _handle(db, workspace, binding)
    if not get_runtime().can_accept_input(handle):
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
    runtime = get_runtime()
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
    paths = tuple(item["path"] for item in attachments)
    image_urls = tuple(item["image_data_url"] for item in attachments if "image_data_url" in item)
    prompt = content.strip()
    if paths:
        prompt += "\n\n已上传到共享工作区的附件：\n" + "\n".join(f"- {path}" for path in paths)
    try:
        result = runtime.send_message(handle, prompt, image_urls)
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
    if not get_runtime().can_accept_input(handle):
        raise DomainError("AGENT_CONVERSATION_BUSY", "请在当前回复完成或暂停后压缩上下文", 409)
    result = get_runtime().condense(handle)
    return {"accepted": True, "cursor": result.cursor}


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
            return _dict(existing)
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
    if migration_provider_id is not None:
        display_title = source.display_title
        runtime_title = source.display_title or "未命名会话"
    else:
        runtime_title = (title or "").strip()[:240] or f"Fork · {source.display_title or '会话'}"
        display_title = runtime_title
    target_id = str(uuid4())
    target = AgentConversationBinding(
        workspace_id=workspace.id,
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
    command = AgentConversationCommand(
        workspace_id=workspace.id,
        binding_id=target.id,
        command_type="FORK",
        idempotency_key=idempotency_key,
        attempt_count=1,
    )
    db.add(command)
    try:
        result = runtime.fork_conversation(
            source_handle,
            target_conversation_id=target_id,
            title=runtime_title,
            from_event_id=event_id,
            expected_source_leaf_event_id=source_identity.event_id,
            reset_metrics=True,
        )
        if (
            result.handle.conversation_id != target_id
            or result.source_conversation_id != source.openhands_conversation_id
            or result.source_event_id != event_id
        ):
            raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "会话分叉身份校验失败", 409)
        identity = runtime.reload_conversation(result.handle)
        if identity.persistence_dir != f"/runtime/state/conversations/{UUID(target_id).hex}":
            raise DomainError("RUNTIME_FORK_IDENTITY_DRIFT", "分叉会话持久化身份校验失败", 409)
        if migration_provider_id is not None:
            active_provider_id = runtime.conversation_context(result.handle).get("provider_id")
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
    return _dict(target)


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
