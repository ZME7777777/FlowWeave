from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import conflict, not_found
from flowweave.shared.models import ModelProvider, NodeAsset, NodeExecutorConfig, ProviderModel
from flowweave.shared.schemas import ModelProviderWrite, ProviderModelWrite
from flowweave.shared.settings import get_settings


def _fernet() -> Fernet:
    configured = get_settings().credentials_master_key.encode()
    key = configured or base64.urlsafe_b64encode(
        hashlib.sha256(get_settings().human_write_token.encode()).digest()
    )
    return Fernet(key)


def provider_auth_headers(item: ModelProvider) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if item.encrypted_api_key:
        headers["Authorization"] = f"Bearer {_fernet().decrypt(item.encrypted_api_key).decode()}"
    return headers


def provider_dict(db: Session, item: ModelProvider) -> dict[str, Any]:
    models = db.scalars(
        select(ProviderModel)
        .where(ProviderModel.provider_id == item.id)
        .order_by(ProviderModel.model_name)
    ).all()
    return {
        "id": item.id,
        "name": item.name,
        "base_url": item.base_url,
        "has_api_key": item.encrypted_api_key is not None,
        "api_key_hint": item.api_key_hint,
        "connection_state": item.connection_state,
        "reference_node_count": len(
            db.scalars(
                select(NodeExecutorConfig.node_asset_id)
                .join(NodeAsset, NodeAsset.id == NodeExecutorConfig.node_asset_id)
                .where(
                    NodeExecutorConfig.model_provider_id == item.id,
                    NodeAsset.deleted_at.is_(None),
                )
            ).all()
        ),
        "available_for_nodes": any(model.enabled and model.is_default for model in models),
        "row_version": item.row_version,
        "models": [
            {
                "id": x.id,
                "model_name": x.model_name,
                "enabled": x.enabled,
                "is_default": x.is_default,
            }
            for x in models
        ],
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def list_providers(db: Session) -> list[dict[str, Any]]:
    return [
        provider_dict(db, x) for x in db.scalars(select(ModelProvider).order_by(ModelProvider.name))
    ]


def get_provider(db: Session, provider_id: str) -> ModelProvider:
    item = db.get(ModelProvider, provider_id)
    if not item:
        raise not_found("model_provider", provider_id)
    return item


def _validate_referenced_models(
    db: Session, provider_id: str, models: list[ProviderModelWrite]
) -> None:
    enabled_names = {model.model_name for model in models if model.enabled}
    referenced_names = {
        name
        for name in db.scalars(
            select(NodeExecutorConfig.model_name).where(
                NodeExecutorConfig.model_provider_id == provider_id,
                NodeExecutorConfig.model_name.is_not(None),
            )
        ).all()
        if name
    }
    unavailable = sorted(referenced_names - enabled_names)
    if unavailable:
        raise conflict(
            "models referenced by node assets cannot be disabled or removed",
            models=unavailable,
        )


def save_provider(
    db: Session, payload: ModelProviderWrite, provider_id: str | None = None
) -> dict[str, Any]:
    if provider_id:
        item = get_provider(db, provider_id)
        if payload.row_version != item.row_version:
            raise conflict(
                "model provider was modified", expected=payload.row_version, actual=item.row_version
            )
        _validate_referenced_models(db, item.id, payload.models)
        item.row_version += 1
    else:
        item = ModelProvider(
            name=payload.name.strip(),
            base_url=payload.base_url.strip().rstrip("/"),
        )
        db.add(item)
        db.flush()
    item.name = payload.name.strip()
    item.base_url = payload.base_url.strip().rstrip("/")
    if payload.api_key:
        item.encrypted_api_key = _fernet().encrypt(payload.api_key.encode())
        item.api_key_hint = f"••••{payload.api_key[-4:]}"
    db.execute(delete(ProviderModel).where(ProviderModel.provider_id == item.id))
    for model in payload.models:
        db.add(ProviderModel(provider_id=item.id, **model.model_dump()))
    finish(db)
    return provider_dict(db, item)


@dataclass(frozen=True, slots=True)
class PromptProviderSnapshot:
    base_url: str
    headers: dict[str, str]
    model: str


def prompt_provider_snapshot(
    db: Session, provider_id: str, requested_model: str | None
) -> PromptProviderSnapshot:
    """Freeze prompt-gate connection data inside a short read transaction."""

    item = get_provider(db, provider_id)
    model = requested_model
    if not model:
        row = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.enabled.is_(True),
                ProviderModel.is_default.is_(True),
            )
        )
        if row is None:
            row = db.scalar(
                select(ProviderModel).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.enabled.is_(True),
                )
            )
        if row is None:
            raise ValueError("Prompt gate provider has no enabled model")
        model = row.model_name
    return PromptProviderSnapshot(
        base_url=item.base_url.rstrip("/"),
        headers=provider_auth_headers(item),
        model=model,
    )


@dataclass(frozen=True, slots=True)
class ProviderConnectionSnapshot:
    provider_id: str
    base_url: str
    headers: dict[str, str]


def provider_connection_snapshot(db: Session, provider_id: str) -> ProviderConnectionSnapshot:
    """Read and decrypt provider connection data inside a short transaction."""

    item = get_provider(db, provider_id)
    return ProviderConnectionSnapshot(
        provider_id=item.id,
        base_url=item.base_url.rstrip("/"),
        headers=provider_auth_headers(item),
    )


def mark_provider_connected(db: Session, provider_id: str) -> None:
    """Persist a successful probe in a separate short write transaction."""

    item = get_provider(db, provider_id)
    item.connection_state = "CONNECTED"
    finish(db)
