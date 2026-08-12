from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from flowweave.modules.model_providers.infrastructure.codex_oauth import (
    CODEX_BASE_URL,
    CodexModelProfile,
    DeviceAuthorization,
    OAuthTokens,
    refresh_access_token,
)
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
        "auth_type": item.auth_type,
        "has_api_key": item.encrypted_api_key is not None,
        "api_key_hint": item.api_key_hint,
        "connection_state": item.connection_state,
        "oauth_connected": item.encrypted_oauth_refresh_token is not None,
        "oauth_account_email": item.oauth_email,
        "oauth_device_pending": bool(
            item.encrypted_oauth_device_auth_id
            and item.oauth_device_expires_at
            and item.oauth_device_expires_at > datetime.now(UTC)
        ),
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
        "available_for_nodes": any(model.enabled and model.is_default for model in models)
        and (item.auth_type == "API_KEY" or item.encrypted_oauth_refresh_token is not None),
        "available_for_prompt_gates": item.auth_type == "API_KEY"
        and any(model.enabled and model.is_default for model in models),
        "row_version": item.row_version,
        "models": [
            {
                "id": x.id,
                "model_name": x.model_name,
                "enabled": x.enabled,
                "is_default": x.is_default,
                "default_reasoning_effort": x.default_reasoning_effort,
                "supported_reasoning_efforts": list(x.supported_reasoning_efforts or []),
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


def require_codex_oauth_provider(db: Session, provider_id: str) -> None:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("device authorization requires a Codex OAuth provider")


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


def delete_providers(db: Session, provider_ids: list[str]) -> dict[str, Any]:
    ids = list(dict.fromkeys(provider_ids))
    items = [get_provider(db, provider_id) for provider_id in ids]
    references: dict[str, list[dict[str, str]]] = {provider_id: [] for provider_id in ids}
    reference_rows = db.execute(
        select(
            NodeExecutorConfig.model_provider_id,
            NodeAsset.id,
            NodeAsset.name,
        )
        .join(NodeAsset, NodeAsset.id == NodeExecutorConfig.node_asset_id)
        .where(
            NodeExecutorConfig.model_provider_id.in_(ids),
            NodeAsset.deleted_at.is_(None),
        )
        .order_by(NodeAsset.name, NodeAsset.id)
    ).tuples()
    for referenced_provider_id, node_id, node_name in reference_rows:
        if referenced_provider_id is not None:
            references[referenced_provider_id].append({"id": node_id, "name": node_name})
    blocked = [
        {
            "id": item.id,
            "name": item.name,
            "relation": "NODE_EXECUTOR",
            "nodes": references[item.id],
        }
        for item in items
        if references[item.id]
    ]
    blocked_ids = {str(item["id"]) for item in blocked}
    deleted_ids = [item.id for item in items if item.id not in blocked_ids]

    deleted_asset_ids = select(NodeAsset.id).where(NodeAsset.deleted_at.is_not(None))
    db.execute(
        update(NodeExecutorConfig)
        .where(
            NodeExecutorConfig.model_provider_id.in_(deleted_ids),
            NodeExecutorConfig.node_asset_id.in_(deleted_asset_ids),
        )
        .values(model_provider_id=None, model_name=None)
    )
    db.execute(delete(ProviderModel).where(ProviderModel.provider_id.in_(deleted_ids)))
    db.execute(delete(ModelProvider).where(ModelProvider.id.in_(deleted_ids)))
    finish(db)
    return {"deleted_ids": deleted_ids, "blocked": blocked}


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
        base_url = (
            CODEX_BASE_URL
            if payload.auth_type == "CODEX_OAUTH"
            else payload.base_url.strip().rstrip("/")
        )
        item = ModelProvider(
            name=payload.name.strip(),
            base_url=base_url,
            auth_type=payload.auth_type,
        )
        db.add(item)
        db.flush()
    item.name = payload.name.strip()
    previous_auth_type = item.auth_type
    item.auth_type = payload.auth_type
    item.base_url = (
        CODEX_BASE_URL
        if payload.auth_type == "CODEX_OAUTH"
        else payload.base_url.strip().rstrip("/")
    )
    item.connection_state = (
        "CONNECTED"
        if payload.auth_type == "CODEX_OAUTH" and item.encrypted_oauth_refresh_token
        else "UNTESTED"
    )
    if payload.auth_type == "API_KEY" and payload.api_key:
        item.encrypted_api_key = _fernet().encrypt(payload.api_key.encode())
        item.api_key_hint = f"••••{payload.api_key[-4:]}"
    if previous_auth_type != payload.auth_type:
        if payload.auth_type == "API_KEY":
            _clear_oauth(item)
        else:
            item.encrypted_api_key = None
            item.api_key_hint = None
    previous_models = {
        model.model_name: model
        for model in db.scalars(
            select(ProviderModel).where(ProviderModel.provider_id == item.id)
        ).all()
    }
    db.execute(delete(ProviderModel).where(ProviderModel.provider_id == item.id))
    for model in payload.models:
        previous = previous_models.get(model.model_name)
        db.add(
            ProviderModel(
                provider_id=item.id,
                **model.model_dump(),
                default_reasoning_effort=(
                    previous.default_reasoning_effort if previous is not None else None
                ),
                supported_reasoning_efforts=(
                    list(previous.supported_reasoning_efforts or []) if previous is not None else []
                ),
            )
        )
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
    if item.auth_type != "API_KEY":
        raise ValueError("Codex OAuth providers cannot be used by prompt gates")
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
    if item.auth_type != "API_KEY":
        raise ValueError("Codex OAuth providers do not expose an OpenAI model-list endpoint")
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


def _clear_device_authorization(item: ModelProvider) -> None:
    item.encrypted_oauth_device_auth_id = None
    item.oauth_user_code = None
    item.oauth_device_expires_at = None
    item.oauth_poll_interval = None


def _clear_oauth(item: ModelProvider) -> None:
    item.encrypted_oauth_access_token = None
    item.encrypted_oauth_refresh_token = None
    item.oauth_access_expires_at = None
    item.oauth_account_id = None
    item.oauth_email = None
    _clear_device_authorization(item)


def save_device_authorization(
    db: Session, provider_id: str, authorization: DeviceAuthorization
) -> dict[str, Any]:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("device authorization requires a Codex OAuth provider")
    item.encrypted_oauth_device_auth_id = _fernet().encrypt(authorization.device_auth_id.encode())
    item.oauth_user_code = authorization.user_code
    item.oauth_device_expires_at = authorization.expires_at
    item.oauth_poll_interval = authorization.interval
    item.connection_state = "AUTHORIZING"
    finish(db)
    return {
        "verification_url": "https://auth.openai.com/codex/device",
        "user_code": authorization.user_code,
        "expires_at": authorization.expires_at.isoformat(),
        "interval": authorization.interval,
    }


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationSnapshot:
    device_auth_id: str
    user_code: str


def device_authorization_snapshot(db: Session, provider_id: str) -> DeviceAuthorizationSnapshot:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("device authorization requires a Codex OAuth provider")
    if (
        not item.encrypted_oauth_device_auth_id
        or not item.oauth_user_code
        or not item.oauth_device_expires_at
    ):
        raise conflict("Codex device authorization has not been started")
    if item.oauth_device_expires_at <= datetime.now(UTC):
        _clear_device_authorization(item)
        item.connection_state = "FAILED"
        finish(db)
        raise conflict("Codex device authorization expired; start again")
    return DeviceAuthorizationSnapshot(
        device_auth_id=_fernet().decrypt(item.encrypted_oauth_device_auth_id).decode(),
        user_code=item.oauth_user_code,
    )


def save_oauth_tokens(db: Session, provider_id: str, tokens: OAuthTokens) -> dict[str, Any]:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("OAuth credentials require a Codex OAuth provider")
    item.encrypted_oauth_access_token = _fernet().encrypt(tokens.access_token.encode())
    item.encrypted_oauth_refresh_token = _fernet().encrypt(tokens.refresh_token.encode())
    item.oauth_access_expires_at = tokens.expires_at
    item.oauth_account_id = tokens.account_id or item.oauth_account_id
    item.oauth_email = tokens.email or item.oauth_email
    item.connection_state = "CONNECTED"
    item.row_version += 1
    _clear_device_authorization(item)
    finish(db)
    return provider_dict(db, item)


def sync_codex_models(
    db: Session, provider_id: str, profiles: list[CodexModelProfile]
) -> dict[str, Any]:
    """Persist an authenticated Codex catalog while preserving user choices."""

    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("model synchronization requires a Codex OAuth provider")

    profile_by_name = {profile.model_name: profile for profile in profiles}
    discovered = list(profile_by_name)
    existing = {
        model.model_name: model
        for model in db.scalars(
            select(ProviderModel).where(ProviderModel.provider_id == provider_id)
        ).all()
    }
    referenced = {
        name
        for name in db.scalars(
            select(NodeExecutorConfig.model_name).where(
                NodeExecutorConfig.model_provider_id == provider_id,
                NodeExecutorConfig.model_name.is_not(None),
            )
        ).all()
        if name
    }
    retained = [name for name in existing if name in referenced and name not in discovered]
    synchronized_names = [*discovered, *retained]

    models: list[ProviderModelWrite] = []
    for name in synchronized_names:
        previous = existing.get(name)
        models.append(
            ProviderModelWrite(
                model_name=name,
                enabled=previous.enabled if previous is not None else True,
                is_default=previous.is_default if previous is not None else False,
            )
        )

    defaults = [model for model in models if model.enabled and model.is_default]
    if len(defaults) > 1:
        for model in defaults[1:]:
            model.is_default = False
    elif not defaults and models:
        first_enabled = next((model for model in models if model.enabled), None)
        if first_enabled is None:
            models[0].enabled = True
            first_enabled = models[0]
        first_enabled.is_default = True

    db.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider_id))
    for model in models:
        profile = profile_by_name.get(model.model_name)
        previous = existing.get(model.model_name)
        db.add(
            ProviderModel(
                provider_id=provider_id,
                **model.model_dump(),
                default_reasoning_effort=(
                    profile.default_reasoning_effort
                    if profile is not None
                    else previous.default_reasoning_effort
                    if previous is not None
                    else None
                ),
                supported_reasoning_efforts=(
                    list(profile.supported_reasoning_efforts)
                    if profile is not None
                    else list(previous.supported_reasoning_efforts or [])
                    if previous is not None
                    else []
                ),
            )
        )
    item.connection_state = "CONNECTED"
    item.row_version += 1
    finish(db)
    return provider_dict(db, item)


def oauth_status(db: Session, provider_id: str) -> dict[str, Any]:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("OAuth status requires a Codex OAuth provider")
    return {
        "state": item.connection_state,
        "connected": item.encrypted_oauth_refresh_token is not None,
        "account_email": item.oauth_email,
    }


def disconnect_oauth(db: Session, provider_id: str) -> dict[str, Any]:
    item = get_provider(db, provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("OAuth disconnect requires a Codex OAuth provider")
    _clear_oauth(item)
    item.connection_state = "UNTESTED"
    item.row_version += 1
    finish(db)
    return provider_dict(db, item)


@dataclass(frozen=True, slots=True)
class CodexRuntimeCredentials:
    access_token: str
    account_id: str | None


def codex_runtime_credentials(db: Session, provider_id: str) -> CodexRuntimeCredentials:
    item = db.get(ModelProvider, provider_id)
    if item is None:
        raise not_found("model_provider", provider_id)
    if item.auth_type != "CODEX_OAUTH":
        raise conflict("provider is not configured for Codex OAuth")
    if not item.encrypted_oauth_access_token or not item.encrypted_oauth_refresh_token:
        raise conflict("Codex OAuth login is required before running this node")
    expires_at = item.oauth_access_expires_at
    if expires_at is not None and expires_at > datetime.now(UTC) + timedelta(minutes=5):
        return CodexRuntimeCredentials(
            access_token=_fernet().decrypt(item.encrypted_oauth_access_token).decode(),
            account_id=item.oauth_account_id,
        )

    # Never hold a database transaction while waiting for OpenAI. The encrypted
    # refresh token is used as an optimistic fence so concurrent workers cannot
    # overwrite a newer rotated credential. Runtime request construction only
    # reaches this path from worker-owned, read-only preparation transactions.
    encrypted_refresh = item.encrypted_oauth_refresh_token
    refresh_token = _fernet().decrypt(encrypted_refresh).decode()
    previous_account_id = item.oauth_account_id
    previous_email = item.oauth_email
    db.rollback()
    tokens = refresh_access_token(refresh_token)
    encrypted_access = _fernet().encrypt(tokens.access_token.encode())
    next_encrypted_refresh = _fernet().encrypt(tokens.refresh_token.encode())
    updated_id = db.scalar(
        update(ModelProvider)
        .where(
            ModelProvider.id == provider_id,
            ModelProvider.auth_type == "CODEX_OAUTH",
            ModelProvider.encrypted_oauth_refresh_token == encrypted_refresh,
        )
        .values(
            encrypted_oauth_access_token=encrypted_access,
            encrypted_oauth_refresh_token=next_encrypted_refresh,
            oauth_access_expires_at=tokens.expires_at,
            oauth_account_id=tokens.account_id or previous_account_id,
            oauth_email=tokens.email or previous_email,
            connection_state="CONNECTED",
            row_version=ModelProvider.row_version + 1,
        )
        .returning(ModelProvider.id)
        .execution_options(synchronize_session=False)
    )
    if updated_id is not None:
        db.commit()
        return CodexRuntimeCredentials(
            access_token=tokens.access_token,
            account_id=tokens.account_id or previous_account_id,
        )

    db.rollback()
    current = get_provider(db, provider_id)
    if not current.encrypted_oauth_access_token:
        raise conflict("Codex OAuth credentials changed during refresh; retry the run")
    return CodexRuntimeCredentials(
        access_token=_fernet().decrypt(current.encrypted_oauth_access_token).decode(),
        account_id=current.oauth_account_id,
    )
