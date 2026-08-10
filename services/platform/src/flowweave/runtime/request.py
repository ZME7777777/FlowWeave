from __future__ import annotations

from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from flowweave.modules.model_providers.application.service import prompt_provider_snapshot
from flowweave.runtime.base import RuntimeProvider, StartAttemptRequest
from flowweave.runtime.workspace import (
    isolated_runtime_workspace_relative,
    materialize_node_workspace,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings


def _provider(db: Session, node: dict[str, Any]) -> RuntimeProvider:
    asset = cast(dict[str, Any], node.get("asset") or {})
    executor = cast(dict[str, Any], asset.get("executor") or {})
    provider_id = str(executor.get("model_provider_id") or "")
    if not provider_id:
        raise DomainError(
            "MODEL_PROVIDER_REQUIRED",
            "The node executor must select a model provider before it can run",
            422,
        )
    selected = prompt_provider_snapshot(
        db,
        provider_id,
        str(executor.get("model_name") or "") or None,
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
    conversation_history: tuple[dict[str, str], ...] = (),
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
        provider=_provider(db, node) if get_settings().runtime_adapter != "mock" else None,
        skills=skills,
        mcp_servers=mcp_servers,
        interaction_mode=interaction_mode,
        startup_prompt=startup_prompt,
        startup_capability_key=startup_capability_key,
        conversation_history=conversation_history,
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
