from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from flowweave.shared.application.transactions import finish
from flowweave.shared.errors import conflict, not_found
from flowweave.shared.models import ModelProvider, NodeAsset, NodeExecutorConfig, ProviderModel
from flowweave.shared.schemas import ModelProviderWrite, ProviderModelWrite
from flowweave.shared.settings import get_settings

_DEVELOPMENT_CREDENTIALS_KEY = b"I84eBL_TIqLl5IVk_DTjGPtUDyVz3pl6pVCHyT8woaE="


def _fernet() -> Fernet:
    configured = get_settings().credentials_master_key.encode()
    return Fernet(configured or _DEVELOPMENT_CREDENTIALS_KEY)


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


def delete_providers(db: Session, provider_ids: list[str]) -> None:
    ids = list(dict.fromkeys(provider_ids))
    items = [get_provider(db, provider_id) for provider_id in ids]
    reference_counts: dict[str, int] = {}
    reference_rows = db.execute(
        select(
            NodeExecutorConfig.model_provider_id,
            func.count(NodeExecutorConfig.node_asset_id),
        )
        .join(NodeAsset, NodeAsset.id == NodeExecutorConfig.node_asset_id)
        .where(
            NodeExecutorConfig.model_provider_id.in_(ids),
            NodeAsset.deleted_at.is_(None),
        )
        .group_by(NodeExecutorConfig.model_provider_id)
    ).tuples()
    for referenced_provider_id, reference_count in reference_rows:
        if referenced_provider_id is not None:
            reference_counts[referenced_provider_id] = reference_count
    blocked = [
        {
            "id": item.id,
            "name": item.name,
            "reference_node_count": reference_counts[item.id],
        }
        for item in items
        if item.id in reference_counts
    ]
    if blocked:
        raise conflict(
            "model providers referenced by node assets cannot be deleted",
            providers=blocked,
        )

    deleted_asset_ids = select(NodeAsset.id).where(NodeAsset.deleted_at.is_not(None))
    db.execute(
        update(NodeExecutorConfig)
        .where(
            NodeExecutorConfig.model_provider_id.in_(ids),
            NodeExecutorConfig.node_asset_id.in_(deleted_asset_ids),
        )
        .values(model_provider_id=None, model_name=None)
    )
    db.execute(delete(ProviderModel).where(ProviderModel.provider_id.in_(ids)))
    db.execute(delete(ModelProvider).where(ModelProvider.id.in_(ids)))
    finish(db)


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
    item.connection_state = "UNTESTED"
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


def mark_provider_connection_state(db: Session, provider_id: str, state: str) -> None:
    """Persist a probe result in a separate short write transaction."""

    item = get_provider(db, provider_id)
    item.connection_state = state
    finish(db)
