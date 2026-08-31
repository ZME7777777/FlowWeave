"""Shared runtime configuration for every platform-managed Agent session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.catalog.public import resolve_version
from flowweave.runtime.base import (
    RuntimeAgentContext,
    RuntimeAgentSpec,
    RuntimeCondenser,
    RuntimeProvider,
    RuntimeTool,
)
from flowweave.runtime.contract import agent_workspace_runtime_contract
from flowweave.runtime.request import resolve_runtime_selection, runtime_provider
from flowweave.runtime.workspace import (
    agent_workspace_capability_marketplace_name,
    materialize_agent_workspace_capabilities,
    materialize_agent_workspace_capability_marketplace,
)
from flowweave.shared.domain.openhands import FIXED_RUNTIME_TOOL_NAMES
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

AgentWorkspace = agent_workspace_host.AgentWorkspace
AgentWorkspaceCapability = agent_workspace_host.AgentWorkspaceCapability

TOOLS = tuple(RuntimeTool(name=name) for name in FIXED_RUNTIME_TOOL_NAMES)
PROJECT_ROOT = "/runtime/workspace/project"
PROACTIVE_COMPACTION_RATIO = 0.8
CONDENSER_MAX_EVENTS = 10_000
MATERIALIZED_CAPABILITY_TYPES = frozenset({"SKILL", "MCP", "PLUGIN"})
PROJECT_ROOT_SYSTEM_CONTEXT = "\n".join(
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


@dataclass(frozen=True, slots=True)
class FrozenSessionCapability:
    version_id: str
    capability_type: str
    capability_key: str
    digest: str
    runtime_config: dict[str, Any]

    def materialization_config(self) -> dict[str, Any]:
        return {
            "capability_version_id": self.version_id,
            "capability_type": self.capability_type,
            "capability_key": self.capability_key,
            "digest": self.digest,
            "normalized_config": dict(self.runtime_config),
            **self.runtime_config,
        }


@dataclass(frozen=True, slots=True)
class FrozenSessionConfig:
    workspace_id: str | None
    model_provider_id: str | None
    model_name: str | None
    reasoning_effort: str | None
    capabilities: tuple[FrozenSessionCapability, ...]


def system_context(working_directory: str) -> str:
    if working_directory == PROJECT_ROOT:
        return PROJECT_ROOT_SYSTEM_CONTEXT
    return PROJECT_ROOT_SYSTEM_CONTEXT + (
        f"\n本次会话的默认工作目录是 {working_directory}；"
        "优先在该目录及其子目录内组织本次工作的文件。"
    )


def default_workspace(db: Session) -> AgentWorkspace | None:
    return db.scalar(select(AgentWorkspace).where(AgentWorkspace.scope_key == "platform-default"))


def resolve_session_config(
    db: Session,
    *,
    model_provider_id: str | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    capability_version_ids: tuple[str, ...] | None = None,
) -> FrozenSessionConfig:
    """Resolve the one default Agent configuration used by every host."""

    workspace = default_workspace(db)
    provider_id = model_provider_id or (workspace.default_model_provider_id if workspace else None)
    selected_model: str | None
    selected_effort: str | None
    if provider_id:
        selected_model, selected_effort = resolve_runtime_selection(
            db,
            {"asset": {"executor": {"model_provider_id": provider_id}}},
            model_name,
            reasoning_effort,
        )
    elif get_settings().runtime_adapter == "mock":
        selected_model = None
        selected_effort = None
    else:
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "请先配置 Agent 工作区的默认模型供应商",
            409,
        )

    frozen: list[FrozenSessionCapability] = []
    if capability_version_ids is not None:
        references = [
            (resolve_version(db, version_id), None) for version_id in capability_version_ids
        ]
        for published, _ in references:
            frozen.append(
                FrozenSessionCapability(
                    version_id=published.version.id,
                    capability_type=published.package.capability_type,
                    capability_key=published.package.capability_key,
                    digest=published.version.digest,
                    runtime_config=published.runtime_config(),
                )
            )
    elif workspace is not None:
        for reference in db.scalars(
            select(AgentWorkspaceCapability)
            .where(AgentWorkspaceCapability.workspace_id == workspace.id)
            .order_by(AgentWorkspaceCapability.position)
        ):
            published = resolve_version(db, reference.capability_version_id)
            if (
                published.package.capability_type != reference.capability_type
                or published.package.capability_key != reference.capability_key
                or published.version.digest != reference.digest
            ):
                raise DomainError(
                    "AGENT_WORKSPACE_CAPABILITY_IDENTITY_DRIFT",
                    "默认 Agent 工作区能力身份校验失败",
                    409,
                )
            frozen.append(
                FrozenSessionCapability(
                    version_id=published.version.id,
                    capability_type=reference.capability_type,
                    capability_key=reference.capability_key,
                    digest=reference.digest,
                    runtime_config=published.runtime_config(),
                )
            )
    return FrozenSessionConfig(
        workspace_id=workspace.id if workspace else None,
        model_provider_id=provider_id,
        model_name=selected_model,
        reasoning_effort=selected_effort,
        capabilities=tuple(frozen),
    )


def freeze_config_on_binding(
    db: Session, binding: AgentConversationBinding, config: FrozenSessionConfig
) -> None:
    for position, capability in enumerate(config.capabilities):
        db.add(
            AgentConversationCapability(
                binding_id=binding.id,
                capability_version_id=capability.version_id,
                capability_type=capability.capability_type,
                capability_key=capability.capability_key,
                digest=capability.digest,
                position=position,
            )
        )


def reserve_flow_node_binding(
    db: Session,
    *,
    runtime_session_id: str,
    flow_run_id: str,
    node_run_id: str,
    node_attempt_id: str,
    working_directory: str,
    create_idempotency_key: str,
    display_title: str | None = None,
    work_directory_version_id: str | None = None,
    config: FrozenSessionConfig | None = None,
    binding_id: str | None = None,
) -> AgentConversationBinding:
    """Reserve and freeze one FlowNode Conversation before Runtime I/O."""

    existing = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.create_idempotency_key == create_idempotency_key
        )
    )
    if existing is not None:
        if existing.host_kind != "FLOW_NODE" or existing.flow_run_id != flow_run_id:
            raise DomainError(
                "AGENT_CONVERSATION_COMMAND_CONFLICT",
                "会话创建请求冲突",
                409,
            )
        return existing
    config = config or resolve_session_config(db)
    binding = AgentConversationBinding(
        id=binding_id or str(uuid4()),
        workspace_id=None,
        runtime_session_id=runtime_session_id,
        host_kind="FLOW_NODE",
        host_id=flow_run_id,
        conversation_scope_id=flow_run_id,
        flow_run_id=flow_run_id,
        node_run_id=node_run_id,
        node_attempt_id=node_attempt_id,
        work_directory_version_id=work_directory_version_id,
        working_directory=working_directory,
        model_provider_id=config.model_provider_id,
        model_name=config.model_name,
        reasoning_effort=config.reasoning_effort,
        streaming_callback_ready=True,
        openhands_conversation_id=str(uuid4()),
        display_title=display_title,
        lifecycle="PROVISIONING",
        create_idempotency_key=create_idempotency_key,
    )
    db.add(binding)
    db.flush()
    freeze_config_on_binding(db, binding, config)
    db.flush()
    return binding


def flow_node_binding_for_attempt(
    db: Session, attempt_id: str, *, require_provisioning: bool = False
) -> AgentConversationBinding:
    binding = db.scalar(
        select(AgentConversationBinding)
        .where(
            AgentConversationBinding.host_kind == "FLOW_NODE",
            AgentConversationBinding.node_attempt_id == attempt_id,
        )
        .order_by(AgentConversationBinding.created_at.desc())
    )
    if binding is None or (require_provisioning and binding.lifecycle != "PROVISIONING"):
        raise DomainError(
            "AGENT_CONVERSATION_CONFIGURATION_MISSING",
            "Agent 会话配置尚未冻结",
            409,
            {"node_attempt_id": attempt_id},
        )
    return binding


def config_from_binding(db: Session, binding: AgentConversationBinding) -> FrozenSessionConfig:
    frozen: list[FrozenSessionCapability] = []
    for reference in db.scalars(
        select(AgentConversationCapability)
        .where(AgentConversationCapability.binding_id == binding.id)
        .order_by(AgentConversationCapability.position)
    ):
        published = resolve_version(db, reference.capability_version_id, include_retired=True)
        if (
            published.package.capability_type != reference.capability_type
            or published.package.capability_key != reference.capability_key
            or published.version.digest != reference.digest
        ):
            raise DomainError(
                "AGENT_CONVERSATION_CAPABILITY_IDENTITY_DRIFT",
                "会话冻结能力身份校验失败",
                409,
            )
        frozen.append(
            FrozenSessionCapability(
                version_id=reference.capability_version_id,
                capability_type=reference.capability_type,
                capability_key=reference.capability_key,
                digest=reference.digest,
                runtime_config=published.runtime_config(),
            )
        )
    return FrozenSessionConfig(
        binding.workspace_id,
        binding.model_provider_id,
        binding.model_name,
        binding.reasoning_effort,
        tuple(frozen),
    )


def provider_for_config(db: Session, config: FrozenSessionConfig) -> RuntimeProvider | None:
    """Resolve credentials only at the live Runtime call boundary."""

    if config.model_provider_id is None:
        if get_settings().runtime_adapter == "mock":
            return None
        raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "Agent 默认模型不可用", 409)
    return runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": config.model_provider_id}}},
        model_name=config.model_name,
        reasoning_effort=config.reasoning_effort,
    )


def frozen_context_suffix(capabilities: tuple[FrozenSessionCapability, ...]) -> str:
    """Render immutable Context versions into OpenHands' native system suffix."""

    sections: list[str] = []
    for capability in capabilities:
        if capability.capability_type != "CONTEXT":
            continue
        text = str(capability.runtime_config.get("text") or "").strip()
        if not text:
            raise DomainError(
                "AGENT_CONTEXT_CAPABILITY_INVALID",
                "已冻结的 Context 内容缺失",
                409,
                {"capability_version_id": capability.version_id},
            )
        sections.append(f"[{capability.capability_key}]\n{text}")
    return "已冻结 Context（仅作系统级会话背景）：\n" + "\n\n".join(sections) if sections else ""


def build_agent_spec(
    config: FrozenSessionConfig,
    *,
    provider: RuntimeProvider | None,
    binding_id: str,
    working_directory: str,
    host_root: Path,
    runtime_root: Path,
    system_message_suffix_append: str = "",
) -> RuntimeAgentSpec:
    materialized = tuple(
        item.materialization_config()
        for item in config.capabilities
        if item.capability_type in MATERIALIZED_CAPABILITY_TYPES
    )
    skills, plugins, mcp_servers = materialize_agent_workspace_capabilities(
        materialized, host_root=host_root, runtime_root=runtime_root
    )
    marketplace_name = agent_workspace_capability_marketplace_name(binding_id)
    materialize_agent_workspace_capability_marketplace(
        None,
        host_root=host_root,
        runtime_root=runtime_root,
        marketplace_name=marketplace_name,
    )
    return RuntimeAgentSpec(
        provider=provider,
        oracle_provider=provider,
        confirmation_policy="NEVER",
        agent_context=RuntimeAgentContext(
            system_message_suffix="\n\n".join(
                part
                for part in (
                    system_context(working_directory),
                    frozen_context_suffix(config.capabilities),
                    system_message_suffix_append.strip(),
                )
                if part
            ),
            registered_marketplaces=(
                {
                    "name": marketplace_name,
                    "source": str(runtime_root / "marketplace"),
                    "auto_load": False,
                },
            ),
        ),
        condenser=RuntimeCondenser(
            kind="LLM_SUMMARIZING",
            max_size=CONDENSER_MAX_EVENTS,
            max_tokens_ratio=PROACTIVE_COMPACTION_RATIO,
            keep_first=4,
        ),
        condenser_provider=provider,
        tools=TOOLS,
        skills=skills,
        plugins=plugins,
        mcp_servers=mcp_servers,
        runtime_contract=agent_workspace_runtime_contract(tuple(tool.name for tool in TOOLS)),
    )


__all__ = (
    "FrozenSessionConfig",
    "PROJECT_ROOT",
    "build_agent_spec",
    "config_from_binding",
    "default_workspace",
    "frozen_context_suffix",
    "freeze_config_on_binding",
    "flow_node_binding_for_attempt",
    "provider_for_config",
    "reserve_flow_node_binding",
    "resolve_session_config",
    "system_context",
)
