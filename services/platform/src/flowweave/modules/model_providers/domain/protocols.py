"""Per-model wire protocol resolution for API-key providers.

One API-key provider owns one base URL and one key.  FlowWeave decides which
protocol, endpoint and authentication header each model uses, so users never
have to split a gateway into one configuration per protocol.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

ModelProtocol = Literal["CHAT_COMPLETIONS", "RESPONSES", "ANTHROPIC"]

ANTHROPIC_VERSION = "2023-06-01"
_CLAUDE = re.compile(r"(^|[/:._-])claude", re.IGNORECASE)
_RESPONSES = re.compile(r"(^|[/:._-])(gpt-?[5-9]|gpt-?\d{2,}|codex|o[1-9](?![a-z]))", re.IGNORECASE)


def infer_model_protocol(model_name: str) -> ModelProtocol:
    """Choose the native protocol of a model family from its name alone."""

    if _CLAUDE.search(model_name):
        return "ANTHROPIC"
    if _RESPONSES.search(model_name):
        return "RESPONSES"
    return "CHAT_COMPLETIONS"


def resolve_model_protocol(model_name: str, override: str | None) -> ModelProtocol:
    if override in {"CHAT_COMPLETIONS", "RESPONSES", "ANTHROPIC"}:
        return override  # type: ignore[return-value]
    return infer_model_protocol(model_name)


def openai_base_url(base_url: str) -> str:
    """Base for ``/chat/completions`` and ``/responses``.

    A bare origin gets the conventional ``/v1``; any explicit path is trusted.
    """

    value = base_url.strip().rstrip("/")
    return f"{value}/v1" if not urlsplit(value).path else value


def anthropic_base_url(base_url: str) -> str:
    """Base handed to the Anthropic client, which appends ``/v1/messages``."""

    value = base_url.strip().rstrip("/")
    return value[:-3] if value.endswith("/v1") else value


def protocol_base_url(base_url: str, protocol: ModelProtocol) -> str:
    return anthropic_base_url(base_url) if protocol == "ANTHROPIC" else openai_base_url(base_url)


def protocol_auth_headers(api_key: str, protocol: ModelProtocol) -> dict[str, str]:
    """Anthropic Messages authenticates with ``x-api-key``; the others use Bearer."""

    if protocol == "ANTHROPIC":
        return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {api_key}"}
