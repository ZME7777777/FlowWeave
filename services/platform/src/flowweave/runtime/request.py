from __future__ import annotations

from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from flowweave.modules.credentials.application.service import issue_optional_runtime_lookup
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
    interaction_mode: Literal["EXECUTION", "COLLABORATION"] = "EXECUTION",
    startup_prompt: str | None = None,
    startup_capability_key: str | None = None,
    output_targets: dict[str, dict[str, str]] | None = None,
    environment_image: str | None = None,
) -> StartAttemptRequest:
    asset = cast(dict[str, Any], node.get("asset") or {})
    skills, mcp_servers, node_workspace_ref = materialize_node_workspace(asset)
    runtime_secrets: dict[str, dict[str, object]] = {}
    if get_settings().runtime_adapter != "mock":
        # Lease durability must not commit the caller's orchestration transaction.
        with Session(bind=db.get_bind()) as lease_db:
            lookup = issue_optional_runtime_lookup(lease_db, audience=attempt_id)
        if lookup is not None:
            lookup_url, scopes = lookup
            runtime_secrets["LARK_ACCESS_TOKEN"] = {
                "kind": "LookupSecret",
                "url": lookup_url,
                "headers": {
                    "Authorization": (f"Bearer {get_settings().credential_internal_api_key}")
                },
                "description": (
                    "Short-lived Lark access token for this execution; "
                    f"granted scopes: {', '.join(scopes) or 'provider default'}"
                ),
            }
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
        output_targets=output_targets or {},
        runtime_secrets=runtime_secrets,
        environment_image=environment_image
        if environment_image is not None
        else str(
            cast(dict[str, Any], asset.get("environment_version") or {}).get("image_digest") or ""
        ),
    )
