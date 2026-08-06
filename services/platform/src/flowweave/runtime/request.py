from __future__ import annotations

import io
import zipfile
from typing import Any, cast

from sqlalchemy.orm import Session

from flowweave.modules.model_providers.application.service import prompt_provider_snapshot
from flowweave.runtime.base import RuntimeProvider, RuntimeSkill, StartAttemptRequest
from flowweave.shared.artifact_store import get_artifact_store
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


def _skill_from_capability(capability: dict[str, Any]) -> RuntimeSkill | None:
    if str(capability.get("capability_type") or "") != "SKILL":
        return None
    normalized = cast(dict[str, Any], capability.get("normalized_config") or {})
    storage_key = str(normalized.get("storage_key") or "")
    entry = str(normalized.get("entry") or "")
    if not storage_key or not entry:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill does not have a persisted package",
            422,
            {"capability_key": capability.get("capability_key")},
        )
    try:
        archive = get_artifact_store().read(storage_key)
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            content = package.read(entry).decode("utf-8")
    except (FileNotFoundError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "A selected Skill package cannot be loaded",
            422,
            {"capability_key": capability.get("capability_key")},
        ) from exc
    return RuntimeSkill(
        name=str(capability.get("capability_key") or entry.rsplit("/", 1)[0]),
        content=content,
        description=str(normalized.get("description") or ""),
        source=entry,
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
    raw_capabilities: object = asset.get("capabilities") or []
    capabilities = (
        [
            cast(dict[str, Any], item)
            for item in cast(list[object], raw_capabilities)
            if isinstance(item, dict)
        ]
        if isinstance(raw_capabilities, list)
        else []
    )
    skills = tuple(
        skill
        for capability in capabilities
        if (skill := _skill_from_capability(capability)) is not None
    )
    return StartAttemptRequest(
        attempt_id=attempt_id,
        execution_key=execution_key,
        node=node,
        bindings=bindings,
        workspace_ref=workspace_ref,
        provider=_provider(db, node) if get_settings().runtime_adapter != "mock" else None,
        skills=skills,
    )
