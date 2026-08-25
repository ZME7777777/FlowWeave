from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from flowweave.shared.errors import DomainError

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = "0.144.1"
DEVICE_VERIFICATION_URL = f"{ISSUER}/codex/device"
DEVICE_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
# This mirrors ``OPENAI_CODEX_MODELS`` in the fixed OpenHands 1.42.0 source
# baseline. The Codex account catalog can also expose product aliases such as
# ``codex-auto-review``. LiteLLM 1.93.1 treats those aliases as lacking native
# Responses streaming and silently makes a non-streaming request, which the
# Codex endpoint rejects. Only expose IDs that the fixed Agent Runtime can
# execute through its formal streaming Responses path.
_OPENHANDS_CODEX_MODELS = frozenset(
    {
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.5",
        "gpt-5.6",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    }
)


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_auth_id: str
    user_code: str
    interval: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime
    account_id: str | None
    email: str | None


@dataclass(frozen=True, slots=True)
class CodexModelProfile:
    model_name: str
    default_reasoning_effort: str | None
    supported_reasoning_efforts: tuple[str, ...]


async def discover_codex_model_profiles(
    client: httpx.AsyncClient, access_token: str, account_id: str | None
) -> list[CodexModelProfile]:
    """Return models and reasoning capabilities for the authenticated account."""

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Originator": "codex_cli_rs",
        "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION} (FlowWeave)",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    try:
        response = await client.get(
            f"{CODEX_BASE_URL}/models",
            params={"client_version": CODEX_CLIENT_VERSION},
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload_object: object = response.json()
        if not isinstance(payload_object, dict):
            raise ValueError("response does not contain a models array")
        payload = cast(dict[str, object], payload_object)
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("response does not contain a models array")
        profiles: dict[str, CodexModelProfile] = {}
        for raw_model in cast(list[object], raw_models):
            if not isinstance(raw_model, dict):
                continue
            model = cast(dict[str, Any], raw_model)
            model_name = ""
            for key in ("slug", "id", "name"):
                value = _string(model, key)
                if value:
                    model_name = value
                    break
            if model_name not in _OPENHANDS_CODEX_MODELS:
                continue
            efforts: list[str] = []
            raw_levels: object = model.get("supported_reasoning_levels")
            if isinstance(raw_levels, list):
                for raw_level in cast(list[object], raw_levels):
                    if isinstance(raw_level, dict):
                        effort = _string(cast(dict[str, Any], raw_level), "effort")
                    elif isinstance(raw_level, str):
                        effort = raw_level.strip()
                    else:
                        effort = ""
                    if effort and effort not in efforts:
                        efforts.append(effort)
            default_effort = _string(model, "default_reasoning_level") or None
            profiles[model_name] = CodexModelProfile(
                model_name=model_name,
                default_reasoning_effort=default_effort,
                supported_reasoning_efforts=tuple(efforts),
            )
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _oauth_error("Unable to list models for the connected Codex account") from exc
    return [profiles[name] for name in sorted(profiles)]


async def discover_codex_models(
    client: httpx.AsyncClient, access_token: str, account_id: str | None
) -> list[str]:
    """Backward-compatible model-name view of the authenticated catalog."""

    return [
        profile.model_name
        for profile in await discover_codex_model_profiles(client, access_token, account_id)
    ]


def _oauth_error(message: str, exc: Exception | None = None) -> DomainError:
    error = DomainError("CODEX_OAUTH_UNAVAILABLE", message, 503)
    if exc is not None:
        error.__cause__ = exc
    return error


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _jwt_claims(value: str) -> dict[str, Any]:
    """Decode claims from a token received directly from OpenAI over TLS.

    The claims are display/routing metadata only. The opaque access token remains
    the sole authentication credential used by the upstream service.
    """

    try:
        encoded = value.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def _tokens(payload: dict[str, Any], previous_refresh_token: str = "") -> OAuthTokens:
    access_token = _string(payload, "access_token")
    refresh_token = _string(payload, "refresh_token") or previous_refresh_token
    if not access_token or not refresh_token:
        raise _oauth_error("Codex OAuth returned incomplete credentials")
    expires_raw = payload.get("expires_in", 3600)
    try:
        expires_in = max(int(expires_raw), 60)
    except (TypeError, ValueError):
        expires_in = 3600
    claims = _jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth")
    account_id = (
        _string(cast(dict[str, Any], auth), "chatgpt_account_id") if isinstance(auth, dict) else ""
    )
    id_claims = _jwt_claims(_string(payload, "id_token"))
    email = _string(id_claims, "email") or _string(claims, "email")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        account_id=account_id or None,
        email=email or None,
    )


async def request_device_authorization(client: httpx.AsyncClient) -> DeviceAuthorization:
    try:
        response = await client.post(
            f"{ISSUER}/api/accounts/deviceauth/usercode",
            json={"client_id": CLIENT_ID},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        device_auth_id = _string(payload, "device_auth_id")
        user_code = _string(payload, "user_code") or _string(payload, "usercode")
        interval = max(int(payload.get("interval", 5)), 1)
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _oauth_error("Unable to start Codex device authorization") from exc
    if not device_auth_id or not user_code:
        raise _oauth_error("Codex device authorization returned an invalid response")
    return DeviceAuthorization(
        device_auth_id=device_auth_id,
        user_code=user_code,
        interval=interval,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


async def poll_device_authorization(
    client: httpx.AsyncClient, device_auth_id: str, user_code: str
) -> OAuthTokens | None:
    try:
        response = await client.post(
            f"{ISSUER}/api/accounts/deviceauth/token",
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if response.status_code in {403, 404}:
            return None
        response.raise_for_status()
        code_payload = cast(dict[str, Any], response.json())
        authorization_code = _string(code_payload, "authorization_code")
        code_verifier = _string(code_payload, "code_verifier")
        if not authorization_code or not code_verifier:
            raise ValueError("missing authorization code or verifier")
        exchanged = await client.post(
            f"{ISSUER}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": authorization_code,
                "redirect_uri": DEVICE_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        exchanged.raise_for_status()
        return _tokens(cast(dict[str, Any], exchanged.json()))
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _oauth_error("Unable to complete Codex device authorization") from exc


def refresh_access_token(refresh_token: str) -> OAuthTokens:
    try:
        with httpx.Client(follow_redirects=False, timeout=15) as client:
            response = client.post(
                f"{ISSUER}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return _tokens(cast(dict[str, Any], response.json()), refresh_token)
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _oauth_error("Unable to refresh Codex authorization") from exc
