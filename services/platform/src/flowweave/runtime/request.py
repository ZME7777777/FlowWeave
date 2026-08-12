from __future__ import annotations

from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.model_providers.application.service import (
    codex_runtime_credentials,
    get_provider,
    prompt_provider_snapshot,
)
from flowweave.modules.model_providers.infrastructure.codex_oauth import CODEX_BASE_URL
from flowweave.runtime.base import RuntimeProvider, StartAttemptRequest
from flowweave.runtime.workspace import (
    isolated_runtime_workspace_relative,
    materialize_hook_config,
    materialize_node_workspace,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import ProviderModel
from flowweave.shared.settings import get_settings


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
    provider = get_provider(db, provider_id)
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
    if effort and (provider.auth_type != "CODEX_OAUTH" or effort not in supported):
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
    )


def resolve_runtime_provider(
    db: Session,
    node: dict[str, Any],
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> RuntimeProvider:
    """Resolve credentials and validated model settings for a live Runtime switch."""

    return runtime_provider(db, node, model_name, reasoning_effort)


def build_runtime_request(
    db: Session,
    *,
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
    conversation_history: tuple[dict[str, str], ...] = (),
    delegation_enabled: bool = False,
    output_targets: dict[str, dict[str, str]] | None = None,
    environment_image: str | None = None,
    environment_id: str | None = None,
    environment_version_id: str | None = None,
    environment_version_no: int | None = None,
    runtime_sandbox_id: str = "",
    runtime_resource_name: str = "",
    runtime_base_url: str = "",
) -> StartAttemptRequest:
    asset = cast(dict[str, Any], node.get("asset") or {})
    skills, mcp_servers, node_workspace_ref = materialize_node_workspace(asset)
    runtime_workspace_relative = isolated_runtime_workspace_relative(
        workspace_ref, node_workspace_ref
    )
    asset_environment = cast(dict[str, Any], asset.get("environment_version") or {})
    return StartAttemptRequest(
        attempt_id=attempt_id,
        execution_key=execution_key,
        node=node,
        bindings=bindings,
        workspace_ref=workspace_ref,
        node_workspace_ref=node_workspace_ref,
        provider=(
            runtime_provider(db, node, model_name, reasoning_effort)
            if get_settings().runtime_adapter != "mock"
            else None
        ),
        skills=skills,
        mcp_servers=mcp_servers,
        hook_config=materialize_hook_config(asset),
        interaction_mode=interaction_mode,
        startup_prompt=startup_prompt,
        startup_capability_key=startup_capability_key,
        conversation_history=conversation_history,
        delegation_enabled=delegation_enabled,
        output_targets=output_targets or {},
        environment_image=environment_image
        if environment_image is not None
        else str(asset_environment.get("image_digest") or ""),
        environment_id=(
            environment_id
            if environment_id is not None
            else str(asset_environment.get("environment_id") or "")
        ),
        environment_version_id=(
            environment_version_id
            if environment_version_id is not None
            else str(asset_environment.get("id") or "")
        ),
        environment_version_no=(
            environment_version_no
            if environment_version_no is not None
            else int(asset_environment.get("version_no") or 0)
        ),
        runtime_workspace_relative=runtime_workspace_relative,
        runtime_sandbox_id=runtime_sandbox_id,
        runtime_resource_name=runtime_resource_name,
        runtime_base_url=runtime_base_url,
    )
