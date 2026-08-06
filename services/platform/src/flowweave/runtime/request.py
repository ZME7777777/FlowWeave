from __future__ import annotations

from typing import Any, cast

from sqlalchemy.orm import Session

from flowweave.modules.model_providers.application.service import prompt_provider_snapshot
from flowweave.runtime.base import RuntimeProvider, StartAttemptRequest
from flowweave.runtime.workspace import materialize_node_workspace
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
) -> StartAttemptRequest:
    asset = cast(dict[str, Any], node.get("asset") or {})
    skills, mcp_servers, node_workspace_ref = materialize_node_workspace(asset)
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
    )
