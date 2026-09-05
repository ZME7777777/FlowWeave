from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.credentials.application.service import credentials_for_agent
from flowweave.modules.model_providers.application.service import (
    codex_runtime_credentials,
    get_provider,
    prompt_provider_snapshot,
)
from flowweave.modules.model_providers.infrastructure.codex_oauth import CODEX_BASE_URL
from flowweave.modules.sandboxes.application.runtime_allocation import (
    capability_materialization_lock,
    node_attempt_workspace_context,
    runtime_allocation_for_flow_run,
    runtime_allocation_for_node_attempt,
)
from flowweave.runtime.base import (
    RuntimeAgentContext,
    RuntimeAgentDefinition,
    RuntimeAgentProfile,
    RuntimeAgentSpec,
    RuntimeBudgets,
    RuntimeCondenser,
    RuntimeContract,
    RuntimeCritic,
    RuntimeProvider,
    RuntimeTool,
    StartAttemptRequest,
)
from flowweave.runtime.contract import normalize_runtime_contract
from flowweave.runtime.workspace import (
    ensure_flow_run_attempt_workspace,
    isolated_runtime_workspace_paths,
    materialize_hook_config,
    materialize_node_workspace,
)
from flowweave.shared.domain.agent_definition import normalize_agent_definition_document
from flowweave.shared.domain.capability_digest import normalized_capability_config
from flowweave.shared.domain.openhands import (
    FIXED_RUNTIME_TOOL_NAMES,
    FIXED_TOOL_CONCURRENCY_LIMIT,
)
from flowweave.shared.domain.runtime_policy import (
    normalize_agent_profile_document,
    normalize_context_policy_document,
    normalize_critic_policy_document,
    normalize_memory_policy_document,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import ProviderModel
from flowweave.shared.settings import get_settings


def runtime_memory_policy(
    node: dict[str, Any], *, scope: Literal["ATTEMPT", "CONVERSATION"]
) -> dict[str, Any] | None:
    """Return the enabled frozen Memory Policy for one Runtime owner scope."""

    raw_agent_spec = node.get("runtime_agent_spec")
    raw_policy = (
        cast(dict[str, Any], raw_agent_spec).get("memory_policy")
        if isinstance(raw_agent_spec, dict)
        else None
    )
    if not isinstance(raw_policy, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy is missing",
            409,
        )
    policy = cast(dict[str, Any], raw_policy)
    raw_config = policy.get("runtime_config")
    try:
        policy_key, config = normalize_memory_policy_document(
            normalized_capability_config(cast(dict[str, Any], raw_config))
            if isinstance(raw_config, dict)
            else raw_config,
            fallback_key=str(policy.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if policy_key != policy.get("capability_key"):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy identity drifted",
            409,
        )
    if not config["enabled"] or scope not in config["scopes"]:
        return None
    return config


def resolve_runtime_selection(
    db: Session,
    node: dict[str, Any],
    requested_model: str | None = None,
    requested_reasoning_effort: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve and validate a runtime model selection.

    With no explicit selection, the node executor's configured model (or the
    provider default) is used. Explicit selections are conversation-scoped.
    """

    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    provider_id = str(executor.get("model_provider_id") or "")
    if not provider_id:
        if (
            get_settings().runtime_adapter == "mock"
            and not requested_model
            and not requested_reasoning_effort
        ):
            return None, None
        raise DomainError(
            "MODEL_PROVIDER_REQUIRED",
            "The node executor must select a model provider before it can run",
            422,
        )
    selected_model = (requested_model or str(executor.get("model_name") or "")).strip()
    if selected_model:
        model = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_name == selected_model,
                ProviderModel.enabled.is_(True),
            )
        )
    else:
        model = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.enabled.is_(True),
                ProviderModel.is_default.is_(True),
            )
        )
    if model is None:
        raise DomainError(
            "MODEL_UNAVAILABLE",
            "The selected model is not enabled for this provider",
            422,
            {"model_name": selected_model or None},
        )
    effort = (requested_reasoning_effort or "").strip() or None
    supported = list(model.supported_reasoning_efforts or [])
    if effort and effort not in supported:
        raise DomainError(
            "REASONING_EFFORT_UNSUPPORTED",
            "The selected reasoning effort is not supported by this model",
            422,
            {
                "model_name": model.model_name,
                "reasoning_effort": effort,
                "supported_reasoning_efforts": supported,
            },
        )
    return model.model_name, effort


def runtime_provider(
    db: Session,
    node: dict[str, Any],
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> RuntimeProvider:
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    provider_id = str(executor.get("model_provider_id") or "")
    selected_model, selected_effort = resolve_runtime_selection(
        db, node, model_name, reasoning_effort
    )
    if selected_model is None:
        raise DomainError(
            "MODEL_UNAVAILABLE",
            "The selected model is unavailable for this runtime",
            422,
        )
    provider = get_provider(db, provider_id)
    if provider.auth_type == "CODEX_OAUTH":
        credentials = codex_runtime_credentials(db, provider_id)
        headers = {
            "originator": "codex_cli_rs",
            "OpenAI-Beta": "responses=experimental",
            "User-Agent": "FlowWeave/OpenHands",
        }
        if credentials.account_id:
            headers["chatgpt-account-id"] = credentials.account_id
        return RuntimeProvider(
            provider_id=provider_id,
            base_url=CODEX_BASE_URL,
            model=selected_model,
            api_key=credentials.access_token,
            auth_type="CODEX_OAUTH",
            extra_headers=headers,
            reasoning_effort=selected_effort,
        )
    selected = prompt_provider_snapshot(
        db,
        provider_id,
        selected_model,
    )
    authorization = selected.headers.get("Authorization", "")
    api_key = authorization.removeprefix("Bearer ").strip()
    if not api_key:
        raise DomainError(
            "MODEL_CREDENTIAL_REQUIRED",
            "The selected model provider does not have an API key",
            422,
        )
    return RuntimeProvider(
        provider_id=provider_id,
        base_url=selected.base_url,
        model=selected.model,
        api_key=api_key,
        reasoning_effort=selected_effort,
    )


def resolve_runtime_provider(
    db: Session,
    node: dict[str, Any],
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> RuntimeProvider:
    """Resolve credentials and validated model settings for a live Runtime switch."""

    return runtime_provider(db, node, model_name, reasoning_effort)


def _runtime_condenser(
    db: Session, node: dict[str, Any], config: dict[str, Any]
) -> tuple[RuntimeCondenser, RuntimeProvider | None]:
    kind = str(config.get("kind") or "NO_OP")
    if kind == "NO_OP":
        return RuntimeCondenser(), None
    if kind != "LLM_SUMMARIZING":
        raise DomainError(
            "SNAPSHOT_INVALID",
            "OpenHands condenser policy is invalid",
            409,
            {"kind": kind},
        )
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    provider_id = str(config.get("model_provider_id") or executor.get("model_provider_id") or "")
    model_name = str(config.get("model_name") or "") or None
    settings = get_settings()
    if not provider_id and settings.runtime_adapter != "mock":
        raise DomainError(
            "MODEL_PROVIDER_REQUIRED",
            "The condenser must select a model provider before it can run",
            422,
        )
    condenser_node = {
        "asset": {
            "executor": {
                "model_provider_id": provider_id,
                "model_name": model_name,
            }
        }
    }
    provider = (
        runtime_provider(db, condenser_node, model_name)
        if settings.runtime_adapter != "mock"
        else None
    )
    return (
        RuntimeCondenser(
            kind="LLM_SUMMARIZING",
            max_size=int(config.get("max_size") or 240),
            max_tokens=(
                int(config["max_tokens"]) if config.get("max_tokens") is not None else None
            ),
            max_tokens_ratio=(
                float(config["max_tokens_ratio"])
                if config.get("max_tokens_ratio") is not None
                else None
            ),
            keep_first=int(config.get("keep_first") or 0),
            minimum_progress=float(config.get("minimum_progress") or 0.1),
            hard_context_reset_max_retries=int(config.get("hard_context_reset_max_retries") or 5),
            hard_context_reset_context_scaling=float(
                config.get("hard_context_reset_context_scaling") or 0.8
            ),
        ),
        provider,
    )


def frozen_memory_policy(
    node: dict[str, Any], *, runtime_scope: Literal["ATTEMPT", "CONVERSATION"]
) -> tuple[bool, list[dict[str, str]]]:
    """Read one frozen Memory Policy without exposing governed content."""

    raw_spec = node.get("runtime_agent_spec")
    spec = cast(dict[str, Any], raw_spec) if isinstance(raw_spec, dict) else {}
    raw_policy = spec.get("memory_policy")
    if not isinstance(raw_policy, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy is missing",
            409,
        )
    entry = cast(dict[str, Any], raw_policy)
    raw_config = entry.get("runtime_config")
    try:
        key, config = normalize_memory_policy_document(
            normalized_capability_config(cast(dict[str, Any], raw_config))
            if isinstance(raw_config, dict)
            else raw_config,
            fallback_key=str(entry.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if key != entry.get("capability_key"):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory Policy identity drifted",
            409,
        )
    enabled = bool(config["enabled"] and runtime_scope in config["scopes"])
    refs = [cast(dict[str, str], item) for item in config["source_refs"]]
    return enabled, refs if enabled else []


def build_runtime_request(
    db: Session,
    *,
    flow_run_id: str,
    runtime_manifest_hash: str,
    attempt_id: str,
    execution_key: str,
    node: dict[str, Any],
    bindings: list[dict[str, Any]],
    workspace_ref: str,
    interaction_mode: Literal["EXECUTION", "COLLABORATION"] = "EXECUTION",
    startup_prompt: str | None = None,
    startup_capability_key: str | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    semantic_history: tuple[dict[str, str], ...] = (),
    output_targets: dict[str, dict[str, str]] | None = None,
    environment_image: str | None = None,
    environment_id: str | None = None,
    environment_version_id: str | None = None,
    environment_version_no: int | None = None,
    runtime_sandbox_id: str = "",
    runtime_resource_name: str = "",
    runtime_base_url: str = "",
    memory_materialized: bool = False,
    agent_spec: RuntimeAgentSpec | None = None,
    conversation_id: str | None = None,
    node_attempt_id: str | None = None,
) -> StartAttemptRequest:
    workspace_context = (
        node_attempt_workspace_context(db, flow_run_id=flow_run_id, node_attempt_id=node_attempt_id)
        if node_attempt_id is not None
        else None
    )
    workspace_root = (
        str(workspace_context.runtime_mount_root)
        if workspace_context is not None
        else "/runtime/workspace/project"
    )
    # Interactive Agent conversations are only hosted by the selected node
    # Attempt. They deliberately do not inherit that node's execution
    # contract, inputs, startup prompt, output targets, memory, hooks, or
    # capabilities. The supplied Agent spec is the same shared session spec
    # used outside FlowRun; the FlowRun contributes only its Runtime and
    # workspace mount. Keep this fast path before all node materialization.
    if interaction_mode == "COLLABORATION" and agent_spec is not None:
        return StartAttemptRequest(
            attempt_id=attempt_id,
            execution_key=execution_key,
            node={},
            bindings=[],
            workspace_ref=workspace_ref,
            conversation_id=conversation_id,
            agent_spec=agent_spec,
            node_workspace_ref="",
            workspace_root=workspace_root,
            interaction_mode="COLLABORATION",
            startup_prompt=None,
            startup_capability_key=None,
            semantic_history=(),
            output_targets={},
            environment_image=environment_image or "",
            environment_id=environment_id or "",
            environment_version_id=environment_version_id or "",
            environment_version_no=environment_version_no or 0,
            runtime_workspace_relative="",
            runtime_working_dir_relative="",
            runtime_working_directory=(
                str(workspace_context.runtime_working_directory)
                if workspace_context is not None
                else ""
            ),
            memory_enabled=False,
            runtime_sandbox_id=runtime_sandbox_id,
            runtime_resource_name=runtime_resource_name,
            runtime_base_url=runtime_base_url,
            conversation_secrets={},
        )
    conversation_secrets, credential_context = credentials_for_agent(db)
    if credential_context and agent_spec is not None:
        context = agent_spec.agent_context
        agent_spec = replace(
            agent_spec,
            agent_context=replace(
                context,
                system_message_suffix="\n\n".join(
                    part for part in (context.system_message_suffix, credential_context) if part
                ),
            ),
        )
    if workspace_context is not None and not workspace_context.attempt_owned:
        raise DomainError(
            "NODE_WORKSPACE_REQUIRES_LEGACY_RUNTIME",
            "The historical Attempt must continue on its FlowRun Runtime",
            409,
            {"node_attempt_id": node_attempt_id},
        )
    runtime_allocation = (
        runtime_allocation_for_node_attempt(
            db,
            flow_run_id=flow_run_id,
            node_attempt_id=node_attempt_id,
            manifest_digest=runtime_manifest_hash,
        )
        if node_attempt_id is not None
        else runtime_allocation_for_flow_run(db, flow_run_id, manifest_digest=runtime_manifest_hash)
    )
    materialization_owner_id = node_attempt_id or flow_run_id
    asset = cast(dict[str, Any], node.get("asset") or {})
    with capability_materialization_lock(runtime_allocation):
        skills, plugins, mcp_servers, node_workspace_ref = materialize_node_workspace(
            asset,
            flow_run_id=materialization_owner_id,
            manifest_digest=runtime_manifest_hash,
        )
        hook_config = materialize_hook_config(
            asset,
            flow_run_id=materialization_owner_id,
            manifest_digest=runtime_manifest_hash,
        )
        if workspace_context is None:
            ensure_flow_run_attempt_workspace(
                flow_run_id=materialization_owner_id,
                asset_id=str(asset.get("id") or ""),
                workspace_ref=workspace_ref,
            )
            runtime_workspace_relative, runtime_working_dir_relative = (
                isolated_runtime_workspace_paths(workspace_ref, node_workspace_ref)
            )
            runtime_working_directory = ""
        else:
            if Path(workspace_ref) != workspace_context.host_working_directory:
                raise DomainError(
                    "RUNTIME_WORKSPACE_INVALID",
                    "The Attempt workspace no longer matches its Runtime allocation",
                    409,
                )
            runtime_workspace_relative = ""
            runtime_working_dir_relative = ""
            runtime_working_directory = str(workspace_context.runtime_working_directory)
    if agent_spec is not None:
        return StartAttemptRequest(
            attempt_id=attempt_id,
            execution_key=execution_key,
            node=node,
            bindings=bindings,
            workspace_ref=workspace_ref,
            conversation_id=conversation_id,
            agent_spec=agent_spec,
            node_workspace_ref=node_workspace_ref,
            workspace_root=workspace_root,
            interaction_mode=interaction_mode,
            startup_prompt=startup_prompt,
            startup_capability_key=startup_capability_key,
            semantic_history=semantic_history,
            output_targets=output_targets or {},
            environment_image=environment_image or "",
            environment_id=environment_id or "",
            environment_version_id=environment_version_id or "",
            environment_version_no=environment_version_no or 0,
            runtime_workspace_relative=runtime_workspace_relative,
            runtime_working_dir_relative=runtime_working_dir_relative,
            runtime_working_directory=runtime_working_directory,
            memory_enabled=False,
            runtime_sandbox_id=runtime_sandbox_id,
            runtime_resource_name=runtime_resource_name,
            runtime_base_url=runtime_base_url,
            conversation_secrets=conversation_secrets,
        )
    raw_agent_spec = node.get("runtime_agent_spec")
    if not isinstance(raw_agent_spec, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec is missing from the frozen Snapshot",
            409,
        )
    frozen_spec = cast(dict[str, Any], raw_agent_spec)
    # Tool Policy was deliberately removed.  A Snapshot carrying one was
    # created under the old, restricted execution model and must be rerun;
    # silently converting it would rewrite its frozen semantics.
    if "tool_policy" in frozen_spec:
        raise DomainError(
            "SNAPSHOT_TOOL_POLICY_REQUIRES_RERUN",
            "This historical Snapshot contains a Tool Policy and must be rerun",
            409,
        )
    try:
        runtime_contract: RuntimeContract = normalize_runtime_contract(
            frozen_spec.get("runtime_contract"),
            required_tools=FIXED_RUNTIME_TOOL_NAMES,
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec contract is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    raw_context_policy = frozen_spec.get("context_policy")
    if not isinstance(raw_context_policy, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Context Policy is missing",
            409,
        )
    context_entry = cast(dict[str, Any], raw_context_policy)
    raw_context_config = context_entry.get("runtime_config")
    try:
        context_key, context_config = normalize_context_policy_document(
            normalized_capability_config(cast(dict[str, Any], raw_context_config))
            if isinstance(raw_context_config, dict)
            else raw_context_config,
            fallback_key=str(context_entry.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Context Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if context_key != context_entry.get("capability_key"):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Context Policy identity drifted",
            409,
        )
    raw_memory_policy = frozen_spec.get("memory_policy")
    raw_critic_policy = frozen_spec.get("critic_policy")
    if not isinstance(raw_memory_policy, dict) or not isinstance(raw_critic_policy, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory or Critic Policy is missing",
            409,
        )
    memory_entry = cast(dict[str, Any], raw_memory_policy)
    critic_entry = cast(dict[str, Any], raw_critic_policy)
    try:
        memory_key, memory_config = normalize_memory_policy_document(
            normalized_capability_config(cast(dict[str, Any], memory_entry.get("runtime_config")))
            if isinstance(memory_entry.get("runtime_config"), dict)
            else memory_entry.get("runtime_config"),
            fallback_key=str(memory_entry.get("capability_key") or ""),
        )
        critic_key, critic_config = normalize_critic_policy_document(
            normalized_capability_config(cast(dict[str, Any], critic_entry.get("runtime_config")))
            if isinstance(critic_entry.get("runtime_config"), dict)
            else critic_entry.get("runtime_config"),
            fallback_key=str(critic_entry.get("capability_key") or ""),
        )
    except ValueError as exc:
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec Memory or Critic Policy is invalid",
            409,
            {"reason": str(exc)},
        ) from exc
    if memory_key != memory_entry.get("capability_key") or critic_key != critic_entry.get(
        "capability_key"
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec policy identity drifted",
            409,
        )
    runtime_memory_scope = "ATTEMPT" if interaction_mode == "EXECUTION" else "CONVERSATION"
    memory_enabled = bool(
        memory_config["enabled"] and runtime_memory_scope in memory_config["scopes"]
    )
    if memory_enabled:
        if not memory_materialized or not environment_image:
            raise DomainError(
                "MEMORY_SOURCE_UNAVAILABLE",
                "Enabled Memory requires an isolated managed Runtime",
                409,
            )
    critic = (
        RuntimeCritic(
            mode=(
                "all_actions" if critic_config["mode"] == "ALL_ACTIONS" else "finish_and_message"
            ),
            success_threshold=float(critic_config["threshold"]),
            max_iterations=int(critic_config["max_refinement_iterations"]),
        )
        if critic_config["enabled"]
        else None
    )
    raw_definitions = frozen_spec.get("agent_definitions", [])
    if not isinstance(raw_definitions, list):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Definitions are invalid",
            409,
        )
    agent_definitions: list[RuntimeAgentDefinition] = []
    for raw_definition in cast(list[object], raw_definitions):
        if not isinstance(raw_definition, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Definition is invalid",
                409,
            )
        raw_config = cast(dict[str, Any], raw_definition).get("runtime_config")
        normalized_config = (
            normalized_capability_config(cast(dict[str, Any], raw_config))
            if isinstance(raw_config, dict)
            else raw_config
        )
        try:
            definition_key = str(cast(dict[str, Any], raw_definition).get("capability_key") or "")
            name, definition = normalize_agent_definition_document(
                normalized_config, fallback_key=definition_key
            )
        except ValueError as exc:
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Definition is invalid",
                409,
                {"reason": str(exc)},
            ) from exc
        definition_tools = tuple(cast(list[str], definition["tools"]))
        if not set(definition_tools) <= set(FIXED_RUNTIME_TOOL_NAMES):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Definition exceeds the fixed Runtime tool set",
                409,
                {"name": name},
            )
        agent_definitions.append(
            RuntimeAgentDefinition(
                name=name,
                description=str(definition["description"]),
                tools=definition_tools,
                system_prompt=str(definition["system_prompt"]),
                when_to_use_examples=tuple(cast(list[str], definition["when_to_use_examples"])),
                permission_mode=cast(str | None, definition["permission_mode"]),
                max_iteration_per_run=cast(int | None, definition["max_iteration_per_run"]),
                max_budget_per_run=cast(float | None, definition["max_budget_per_run"]),
            )
        )
    if frozen_spec.get("confirmation_policy") not in (None, "NEVER"):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec must use NeverConfirm",
            409,
        )
    raw_condenser_value = frozen_spec.get("condenser")
    if not isinstance(raw_condenser_value, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec condenser policy is invalid",
            409,
        )
    raw_condenser = cast(dict[str, Any], raw_condenser_value)
    raw_budgets = frozen_spec.get("budgets")
    if not isinstance(raw_budgets, dict):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec budgets are invalid",
            409,
        )
    max_iterations = cast(dict[str, Any], raw_budgets).get("max_iterations")
    if (
        not isinstance(max_iterations, int)
        or isinstance(max_iterations, bool)
        or not (1 <= max_iterations <= 1000)
    ):
        raise DomainError(
            "SNAPSHOT_MANIFEST_INVALID",
            "Runtime Agent Spec iteration budget is invalid",
            409,
        )
    raw_profile = frozen_spec.get("agent_profile")
    agent_profile: RuntimeAgentProfile | None = None
    if raw_profile is not None:
        if not isinstance(raw_profile, dict):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Profile is invalid",
                409,
            )
        profile_entry = cast(dict[str, Any], raw_profile)
        raw_profile_config = profile_entry.get("runtime_config")
        profile_config = (
            normalized_capability_config(cast(dict[str, Any], raw_profile_config))
            if isinstance(raw_profile_config, dict)
            else raw_profile_config
        )
        try:
            profile_key, normalized_profile = normalize_agent_profile_document(
                profile_config,
                fallback_key=str(profile_entry.get("capability_key") or ""),
            )
        except ValueError as exc:
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Profile is invalid",
                409,
                {"reason": str(exc)},
            ) from exc
        expected_references = {
            "context_policy_version_id": str(context_entry.get("capability_version_id") or ""),
            "memory_policy_version_id": str(memory_entry.get("capability_version_id") or ""),
            "critic_policy_version_id": str(critic_entry.get("capability_version_id") or ""),
        }
        version_id = str(profile_entry.get("capability_version_id") or "")
        digest = str(profile_entry.get("digest") or "")
        content_hash = str(profile_entry.get("content_hash") or "")
        if (
            profile_entry.get("capability_type") != "AGENT_PROFILE"
            or len(version_id) != 36
            or len(digest) != 64
            or len(content_hash) != 64
            or profile_key != profile_entry.get("capability_key")
            or any(
                normalized_profile.get(field) != expected
                for field, expected in expected_references.items()
            )
            or normalized_profile.get("max_iterations") != max_iterations
        ):
            raise DomainError(
                "SNAPSHOT_MANIFEST_INVALID",
                "Runtime Agent Profile drifted from the materialized Agent Spec",
                409,
            )
        agent_profile = RuntimeAgentProfile(
            capability_version_id=version_id,
            capability_key=profile_key,
            digest=digest,
            content_hash=content_hash,
            schema_version=int(normalized_profile["schema_version"]),
            source_profile_id=cast(str | None, normalized_profile["source_profile_id"]),
            source_revision=int(normalized_profile["source_revision"]),
        )
    condenser, condenser_provider = _runtime_condenser(db, node, raw_condenser)
    provider = (
        runtime_provider(db, node, model_name, reasoning_effort)
        if get_settings().runtime_adapter != "mock"
        else None
    )
    agent_spec = RuntimeAgentSpec(
        schema_version=int(frozen_spec.get("schema_version") or 0),
        agent_kind=cast(Literal["OPENHANDS", "ACP"], frozen_spec.get("agent_kind")),
        runtime_contract=runtime_contract,
        agent_profile=agent_profile,
        provider=provider,
        tools=tuple(RuntimeTool(name=name) for name in FIXED_RUNTIME_TOOL_NAMES),
        tool_concurrency_limit=FIXED_TOOL_CONCURRENCY_LIMIT,
        agent_context=RuntimeAgentContext(
            system_message_suffix=str(context_config["system_message_suffix"]),
            user_message_suffix=str(context_config["user_message_suffix"]),
            load_user_skills=False,
            load_public_skills=False,
            marketplace_path=None,
            registered_marketplaces=(),
            load_project_skills=False,
            load_memory=memory_enabled,
            disabled_skills=tuple(cast(list[str], context_config["disabled_skills"])),
        ),
        agent_definitions=tuple(agent_definitions),
        plugins=plugins,
        skills=skills,
        mcp_servers=mcp_servers,
        hook_config=hook_config,
        confirmation_policy="NEVER",
        condenser=condenser,
        condenser_provider=condenser_provider,
        critic=critic,
        budgets=RuntimeBudgets(max_iterations=max_iterations),
    )
    return StartAttemptRequest(
        attempt_id=attempt_id,
        execution_key=execution_key,
        node=node,
        bindings=bindings,
        workspace_ref=workspace_ref,
        agent_spec=agent_spec,
        node_workspace_ref=node_workspace_ref,
        workspace_root=workspace_root,
        interaction_mode=interaction_mode,
        startup_prompt=startup_prompt,
        startup_capability_key=startup_capability_key,
        semantic_history=semantic_history,
        output_targets=output_targets or {},
        environment_image=environment_image or "",
        environment_id=environment_id or "",
        environment_version_id=environment_version_id or "",
        environment_version_no=environment_version_no or 0,
        runtime_workspace_relative=runtime_workspace_relative,
        runtime_working_dir_relative=runtime_working_dir_relative,
        runtime_working_directory=runtime_working_directory,
        memory_enabled=memory_enabled,
        runtime_sandbox_id=runtime_sandbox_id,
        runtime_resource_name=runtime_resource_name,
        runtime_base_url=runtime_base_url,
        conversation_secrets=conversation_secrets,
    )
