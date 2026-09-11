"""Shared runtime configuration for every platform-managed Agent session."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.agent_workspaces import public as agent_workspace_host
from flowweave.modules.catalog.public import (
    hold_session_memory_references,
    resolve_snapshot_memory,
    resolve_version,
)
from flowweave.modules.model_providers.application.service import has_connected_default_model
from flowweave.runtime.base import (
    RuntimeAgentContext,
    RuntimeAgentDefinition,
    RuntimeAgentSpec,
    RuntimeBudgets,
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
    materialize_agent_workspace_hook_config,
    materialize_runtime_memory,
)
from flowweave.shared.domain.agent_definition import normalize_agent_definition_document
from flowweave.shared.domain.openhands import FIXED_RUNTIME_TOOL_NAMES
from flowweave.shared.domain.runtime_policy import normalize_memory_policy_document
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

AgentWorkspace = agent_workspace_host.AgentWorkspace
AgentWorkspaceCapability = agent_workspace_host.AgentWorkspaceCapability

TOOLS = tuple(RuntimeTool(name=name) for name in FIXED_RUNTIME_TOOL_NAMES)
PROJECT_ROOT = "/runtime/workspace/project"
PROACTIVE_COMPACTION_RATIO = 0.8
CONDENSER_MAX_EVENTS = 10_000
AGENT_WORKSPACE_MAX_ITERATIONS = 300
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
CONVERSATION_COLLABORATION_CONTEXT = "\n".join(
    (
        "协作节奏：向用户展示的是阶段目标和可验证进展，而不是每一条工具调用的预告或复述。",
        "仅在开始执行、计划或风险发生变化、完成关键阶段、需要用户决定或最终交付时，给出简短进展；不要在普通工具调用前逐条解释命令。",
        (
            "独立且只读的检查可以在同一轮模型输出中并行发起；存在数据依赖、写入、Git 提交/推送、"
            "部署、权限确认或其他风险的动作必须串行，先读取并验证前一步结果。"
        ),
        "多步骤工作使用原生任务跟踪器维护计划；只在建立、实质调整或完成关键任务时更新，不要把任务跟踪器当作每次工具调用的说明。",
        "最终答复应优先说明完成情况、关键结果、已运行的验证以及仍存在的风险或需要用户决定的事项。",
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
class FrozenModelFallback:
    """An ordered, immutable alternate LLM identity for one session."""

    model_provider_id: str
    model_name: str
    reasoning_effort: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "model_provider_id": self.model_provider_id,
            "model_name": self.model_name,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class FrozenSessionConfig:
    workspace_id: str | None
    model_provider_id: str | None
    model_name: str | None
    reasoning_effort: str | None
    capabilities: tuple[FrozenSessionCapability, ...]
    fallback_models: tuple[FrozenModelFallback, ...] = ()


def materialize_frozen_memory(
    db: Session,
    config: FrozenSessionConfig,
    *,
    runtime_scope: Literal["ATTEMPT", "CONVERSATION"],
    snapshot_id: str,
    flow_run_id: str,
    manifest_digest: str,
    workspace_ref: str,
    project_root: Path,
    capability_root: Path,
) -> bool:
    """Expose only a frozen session Memory bundle to OpenHands.

    Agent Workspace configuration intentionally has no Run Snapshot and never
    calls this helper.  That keeps OpenHands' ambient user-memory tier disabled
    outside FlowRun execution/conversation paths.
    """

    policies = [
        capability
        for capability in config.capabilities
        if capability.capability_type == "MEMORY_POLICY"
    ]
    if not policies:
        return False
    if len(policies) != 1:
        raise DomainError(
            "AGENT_MEMORY_POLICY_CONFLICT",
            "一个会话只能冻结一个 Memory Policy",
            409,
        )
    policy = policies[0]
    try:
        policy_key, document = normalize_memory_policy_document(
            policy.runtime_config, fallback_key=policy.capability_key
        )
    except ValueError as exc:
        raise DomainError(
            "AGENT_MEMORY_POLICY_INVALID",
            "已冻结的 Memory Policy 无效",
            409,
            {"capability_version_id": policy.version_id},
        ) from exc
    if policy_key != policy.capability_key:
        raise DomainError(
            "AGENT_MEMORY_POLICY_IDENTITY_DRIFT",
            "已冻结的 Memory Policy 身份校验失败",
            409,
        )
    if not document["enabled"] or runtime_scope not in document["scopes"]:
        return False
    source_refs = list(document["source_refs"])
    hold_session_memory_references(db, snapshot_id=snapshot_id, source_refs=source_refs)
    materials = resolve_snapshot_memory(
        db,
        snapshot_id=snapshot_id,
        source_refs=source_refs,
        allowed_scopes={"USER", "PROJECT"},
    )
    materialize_runtime_memory(
        flow_run_id=flow_run_id,
        manifest_digest=manifest_digest,
        workspace_ref=workspace_ref,
        materials=materials,
        project_root=project_root,
        capability_root=capability_root,
    )
    return True


def system_context(working_directory: str) -> str:
    return PROJECT_ROOT_SYSTEM_CONTEXT.replace(PROJECT_ROOT, working_directory)


def default_workspace(db: Session) -> AgentWorkspace | None:
    return db.scalar(select(AgentWorkspace).where(AgentWorkspace.scope_key == "platform-default"))


def resolve_session_config(
    db: Session,
    *,
    model_provider_id: str | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    fallback_models: tuple[dict[str, object], ...] = (),
    capability_version_ids: tuple[str, ...] | None = None,
) -> FrozenSessionConfig:
    """Resolve an explicitly selected Agent configuration.

    Model identity is part of the session contract.  It must be supplied by
    the caller and is never inferred from an Agent Workspace preference.
    """

    workspace = default_workspace(db)
    primary_provider_id = model_provider_id
    selected_model: str | None
    selected_effort: str | None
    if primary_provider_id and model_name:
        selected_model, selected_effort = resolve_runtime_selection(
            db,
            {"asset": {"executor": {"model_provider_id": primary_provider_id}}},
            model_name,
            reasoning_effort,
        )
    else:
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "必须显式选择会话模型供应商和模型",
            409,
        )

    if len(fallback_models) > 3:
        raise DomainError(
            "AGENT_FALLBACK_POLICY_INVALID",
            "At most three fallback models may be frozen for one session",
            422,
        )
    resolved_fallbacks: list[FrozenModelFallback] = []
    seen_fallbacks: set[tuple[str, str, str | None]] = set()
    for raw_fallback in fallback_models:
        if not isinstance(raw_fallback, dict):
            raise DomainError(
                "AGENT_FALLBACK_POLICY_INVALID",
                "Fallback model entries must be objects",
                422,
            )
        fallback_provider_id = str(raw_fallback.get("model_provider_id") or "").strip()
        fallback_model = str(raw_fallback.get("model_name") or "").strip()
        raw_effort = raw_fallback.get("reasoning_effort")
        fallback_effort = str(raw_effort).strip() if raw_effort else None
        if (
            not fallback_provider_id
            or not fallback_model
            or not has_connected_default_model(db, fallback_provider_id)
        ):
            raise DomainError(
                "AGENT_FALLBACK_MODEL_UNAVAILABLE",
                "Fallback models must be connected, enabled, and have runtime credentials",
                409,
            )
        resolved_model, resolved_effort = resolve_runtime_selection(
            db,
            {"asset": {"executor": {"model_provider_id": fallback_provider_id}}},
            fallback_model,
            fallback_effort,
        )
        if resolved_model is None:
            raise DomainError(
                "AGENT_FALLBACK_MODEL_UNAVAILABLE", "Fallback model is unavailable", 409
            )
        identity = (fallback_provider_id, resolved_model, resolved_effort)
        if identity in seen_fallbacks:
            raise DomainError(
                "AGENT_FALLBACK_MODEL_DUPLICATE", "Fallback models must be unique", 422
            )
        if identity == (primary_provider_id, selected_model, selected_effort):
            raise DomainError(
                "AGENT_FALLBACK_MODEL_PRIMARY",
                "A fallback model must differ from the primary model",
                422,
            )
        seen_fallbacks.add(identity)
        resolved_fallbacks.append(FrozenModelFallback(*identity))

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
        model_provider_id=primary_provider_id,
        model_name=selected_model,
        reasoning_effort=selected_effort,
        capabilities=tuple(frozen),
        fallback_models=tuple(resolved_fallbacks),
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
    openhands_conversation_id: str | None = None,
) -> AgentConversationBinding:
    """Reserve and freeze one FlowNode Conversation before Runtime I/O."""

    if config is None or not config.model_provider_id or not config.model_name:
        raise DomainError(
            "AGENT_MODEL_CONFIGURATION_REQUIRED",
            "节点会话必须显式选择模型供应商和模型",
            409,
        )
    existing = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.create_idempotency_key == create_idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.host_kind != "FLOW_NODE"
            or existing.flow_run_id != flow_run_id
            or existing.node_attempt_id != node_attempt_id
            or existing.runtime_session_id != runtime_session_id
        ):
            raise DomainError(
                "AGENT_CONVERSATION_COMMAND_CONFLICT",
                "会话创建请求冲突",
                409,
            )
        return existing
    binding = AgentConversationBinding(
        id=binding_id or str(uuid4()),
        workspace_id=None,
        runtime_session_id=runtime_session_id,
        host_kind="FLOW_NODE",
        host_id=flow_run_id,
        conversation_scope_id=node_attempt_id,
        flow_run_id=flow_run_id,
        node_run_id=node_run_id,
        node_attempt_id=node_attempt_id,
        work_directory_version_id=work_directory_version_id,
        working_directory=working_directory,
        model_provider_id=config.model_provider_id,
        model_name=config.model_name,
        reasoning_effort=config.reasoning_effort,
        fallback_models_json=[item.as_dict() for item in config.fallback_models],
        streaming_callback_ready=True,
        openhands_conversation_id=openhands_conversation_id or str(uuid4()),
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
            AgentConversationBinding.create_idempotency_key == f"attempt-runtime:{attempt_id}",
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
    raw_fallbacks = binding.fallback_models_json or []
    if not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > 3:
        raise DomainError("AGENT_FALLBACK_POLICY_INVALID", "Frozen fallback policy is invalid", 409)
    fallback_models: list[FrozenModelFallback] = []
    seen_fallbacks: set[tuple[str, str, str | None]] = set()
    primary_identity = (
        binding.model_provider_id,
        binding.model_name,
        binding.reasoning_effort,
    )
    for item in raw_fallbacks:
        if not isinstance(item, dict):
            raise DomainError(
                "AGENT_FALLBACK_POLICY_INVALID", "Frozen fallback policy is invalid", 409
            )
        provider_id = item.get("model_provider_id")
        model_name = item.get("model_name")
        reasoning_effort = item.get("reasoning_effort")
        if (
            not isinstance(provider_id, str)
            or not provider_id.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
            or reasoning_effort is not None
            and not isinstance(reasoning_effort, str)
        ):
            raise DomainError(
                "AGENT_FALLBACK_POLICY_INVALID", "Frozen fallback policy is invalid", 409
            )
        identity = (
            provider_id.strip(),
            model_name.strip(),
            reasoning_effort.strip() if reasoning_effort else None,
        )
        if identity == primary_identity or identity in seen_fallbacks:
            raise DomainError(
                "AGENT_FALLBACK_POLICY_INVALID", "Frozen fallback policy is invalid", 409
            )
        seen_fallbacks.add(identity)
        fallback_models.append(FrozenModelFallback(*identity))
    return FrozenSessionConfig(
        workspace_id=binding.workspace_id,
        model_provider_id=binding.model_provider_id,
        model_name=binding.model_name,
        reasoning_effort=binding.reasoning_effort,
        capabilities=tuple(frozen),
        fallback_models=tuple(fallback_models),
    )


def provider_for_config(db: Session, config: FrozenSessionConfig) -> RuntimeProvider | None:
    """Resolve credentials only at the live Runtime call boundary."""

    if config.model_provider_id is None:
        if get_settings().runtime_adapter == "mock":
            return None
        raise DomainError("AGENT_MODEL_CONFIGURATION_REQUIRED", "会话缺少冻结模型", 409)
    primary = runtime_provider(
        db,
        {"asset": {"executor": {"model_provider_id": config.model_provider_id}}},
        model_name=config.model_name,
        reasoning_effort=config.reasoning_effort,
    )
    fallback_providers = tuple(
        runtime_provider(
            db,
            {"asset": {"executor": {"model_provider_id": fallback.model_provider_id}}},
            model_name=fallback.model_name,
            reasoning_effort=fallback.reasoning_effort,
        )
        for fallback in config.fallback_models
    )
    return (
        primary
        if not fallback_providers
        else replace(primary, fallback_providers=fallback_providers)
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


def frozen_agent_definitions(
    capabilities: tuple[FrozenSessionCapability, ...],
) -> tuple[RuntimeAgentDefinition, ...]:
    """Compile creation-scoped Agent Definitions into the native request."""

    definitions: list[RuntimeAgentDefinition] = []
    names: set[str] = set()
    for capability in capabilities:
        if capability.capability_type != "AGENT_DEFINITION":
            continue
        try:
            name, document = normalize_agent_definition_document(
                capability.runtime_config, fallback_key=capability.capability_key
            )
        except ValueError as exc:
            raise DomainError(
                "AGENT_DEFINITION_CAPABILITY_INVALID",
                "已冻结的 Agent Definition 无效",
                409,
                {"capability_version_id": capability.version_id},
            ) from exc
        if name in names:
            raise DomainError(
                "AGENT_DEFINITION_CAPABILITY_CONFLICT",
                "一个会话不能冻结同名 Agent Definition",
                409,
                {"name": name},
            )
        names.add(name)
        definitions.append(
            RuntimeAgentDefinition(
                name=name,
                description=str(document["description"]),
                tools=tuple(str(item) for item in document["tools"]),
                system_prompt=str(document["system_prompt"]),
                when_to_use_examples=tuple(str(item) for item in document["when_to_use_examples"]),
                permission_mode=str(document["permission_mode"]),
                max_iteration_per_run=document["max_iteration_per_run"],
                max_budget_per_run=document["max_budget_per_run"],
            )
        )
    return tuple(definitions)


def build_agent_spec(
    config: FrozenSessionConfig,
    *,
    provider: RuntimeProvider | None,
    binding_id: str,
    working_directory: str,
    host_root: Path,
    runtime_root: Path,
    system_message_suffix_append: str = "",
    load_memory: bool = False,
) -> RuntimeAgentSpec:
    materialized = tuple(
        item.materialization_config()
        for item in config.capabilities
        if item.capability_type in MATERIALIZED_CAPABILITY_TYPES
    )
    skills, plugins, mcp_servers = materialize_agent_workspace_capabilities(
        materialized, host_root=host_root, runtime_root=runtime_root
    )
    hook_config = materialize_agent_workspace_hook_config(
        tuple(
            item.materialization_config()
            for item in config.capabilities
            if item.capability_type == "HOOK"
        ),
        host_root=host_root,
        runtime_root=runtime_root,
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
        confirmation_policy="NEVER",
        agent_context=RuntimeAgentContext(
            system_message_suffix="\n\n".join(
                part
                for part in (
                    system_context(working_directory),
                    CONVERSATION_COLLABORATION_CONTEXT,
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
            load_memory=load_memory,
        ),
        condenser=RuntimeCondenser(
            kind="LLM_SUMMARIZING",
            max_size=CONDENSER_MAX_EVENTS,
            max_tokens_ratio=PROACTIVE_COMPACTION_RATIO,
            keep_first=4,
        ),
        condenser_provider=provider,
        budgets=RuntimeBudgets(max_iterations=AGENT_WORKSPACE_MAX_ITERATIONS),
        tools=TOOLS,
        skills=skills,
        plugins=plugins,
        mcp_servers=mcp_servers,
        hook_config=hook_config,
        agent_definitions=frozen_agent_definitions(config.capabilities),
        runtime_contract=agent_workspace_runtime_contract(tuple(tool.name for tool in TOOLS)),
    )


__all__ = (
    "FrozenSessionConfig",
    "FrozenModelFallback",
    "PROJECT_ROOT",
    "build_agent_spec",
    "config_from_binding",
    "default_workspace",
    "frozen_agent_definitions",
    "frozen_context_suffix",
    "freeze_config_on_binding",
    "flow_node_binding_for_attempt",
    "materialize_frozen_memory",
    "provider_for_config",
    "reserve_flow_node_binding",
    "resolve_session_config",
    "system_context",
)
