"""One-shot, FlowWeave-only titles for lazy Agent Conversation bootstrap."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, cast

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.model_providers.public import TitleProviderSnapshot, title_provider_snapshot
from flowweave.modules.tasks.public import Lease, lease_is_current
from flowweave.shared.models import BackgroundTask

_SYSTEM_PROMPT = (
    "根据用户第一条输入生成一个简洁、准确的中文会话标题。"
    "只返回标题本身：不使用 Markdown、引号、前后缀或换行；最多 24 个中文字符或 60 个字符。"
)
_MECHANICAL_TITLE = re.compile(
    r"^(?:未命名会话|新会话)\s*(?:[0-9]+|[一二三四五六七八九十]+)?$",
    re.IGNORECASE,
)
_logger = logging.getLogger(__name__)


def _clean_title(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split()).strip(" '“”‘’\"")
    cleaned = cleaned[:80]
    if not cleaned or _MECHANICAL_TITLE.fullmatch(cleaned):
        return fallback
    return cleaned


def _failure_reason(exc: Exception) -> str:
    """Return a searchable failure class without recording model/user content."""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_status_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "http_timeout"
    if isinstance(exc, httpx.HTTPError):
        return "http_transport_error"
    if isinstance(exc, (KeyError, IndexError, TypeError)):
        return "malformed_provider_response"
    if isinstance(exc, ValueError):
        message = str(exc)
        if message == "title provider metadata is invalid":
            return "invalid_provider_metadata"
        if message == "title provider returned no usable title":
            return "empty_or_mechanical_title"
        if message == "Responses title response did not complete":
            return "responses_not_completed"
        if message == "Responses title response did not contain output text":
            return "responses_missing_output_text"
        if message.startswith("Responses title request failed:"):
            return "responses_request_failed"
    return "unknown_title_generation_error"


def _chat_title(snapshot: TitleProviderSnapshot, first_message: str) -> str:
    payload = {
        "model": snapshot.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": first_message},
        ],
    }
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(
            f"{snapshot.base_url}/chat/completions", headers=snapshot.headers, json=payload
        )
        response.raise_for_status()
    body = cast(dict[str, Any], response.json())
    return str(body["choices"][0]["message"]["content"])


def _responses_title(snapshot: TitleProviderSnapshot, first_message: str) -> str:
    payload = {
        "model": snapshot.model,
        "stream": True,
        "store": False,
        "reasoning": {"effort": "low"},
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": _SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": first_message}],
            },
        ],
    }
    deltas: list[str] = []
    completed: dict[str, Any] | None = None
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        with client.stream(
            "POST",
            f"{snapshot.base_url}/responses",
            headers={**snapshot.headers, "Accept": "text/event-stream"},
            json=payload,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if not raw or raw == "[DONE]":
                    continue
                event = cast(dict[str, Any], json.loads(raw))
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        deltas.append(delta)
                elif event_type == "response.completed":
                    value = event.get("response")
                    if isinstance(value, dict):
                        completed = cast(dict[str, Any], value)
                elif event_type in {"error", "response.failed"}:
                    error = event.get("error") or event.get("response") or event
                    raise ValueError(f"Responses title request failed: {error}")
    if deltas:
        return "".join(deltas)
    if completed is None:
        raise ValueError("Responses title response did not complete")
    direct = completed.get("output_text")
    if isinstance(direct, str):
        return direct
    output = completed.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses title response did not contain output text")
    for raw_item in cast(list[object], output):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, object], raw_item)
        contents = item.get("content")
        if not isinstance(contents, list):
            continue
        for raw_content in cast(list[object], contents):
            if not isinstance(raw_content, dict):
                continue
            content = cast(dict[str, object], raw_content)
            text = content.get("text")
            if isinstance(text, str):
                return text
    raise ValueError("Responses title response did not contain output text")


def generate_title(snapshot: TitleProviderSnapshot, first_message: str) -> str:
    """Issue the provider request independently from OpenHands and its events."""

    if snapshot.protocol == "RESPONSES":
        return _responses_title(snapshot, first_message)
    return _chat_title(snapshot, first_message)


def _redact_task_seed(db: Session, lease: Lease, generation: int) -> None:
    """Remove the transient message seed as soon as the one-shot task finishes."""

    db.execute(
        update(BackgroundTask)
        .where(
            BackgroundTask.id == lease.task_id,
            BackgroundTask.lease_owner == lease.owner,
            BackgroundTask.lease_generation == lease.generation,
        )
        .values(payload_json={"title_generation": generation})
    )


def process_agent_conversation_title(
    db: Session, binding_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    """Resolve at most one display title, preserving manual names with a CAS."""

    generation = payload.get("title_generation")
    first_message = payload.get("first_message")
    fallback = _clean_title(payload.get("fallback_title"), "新会话")
    if not isinstance(generation, int) or generation < 1 or not isinstance(first_message, str):
        _redact_task_seed(db, lease, generation if isinstance(generation, int) else 0)
        return

    binding = db.get(AgentConversationBinding, binding_id)
    if (
        binding is None
        or binding.lifecycle != "ACTIVE"
        or binding.title_state != "PENDING"
        or binding.title_generation != generation
    ):
        _redact_task_seed(db, lease, generation)
        return

    state = "GENERATED"
    title = fallback
    provider_id: object = None
    model_name: object = None
    try:
        provider_id = payload.get("model_provider_id")
        model_name = payload.get("model_name")
        if not isinstance(provider_id, str) or not isinstance(model_name, str):
            raise ValueError("title provider metadata is invalid")
        title = _clean_title(
            generate_title(title_provider_snapshot(db, provider_id, model_name), first_message),
            fallback,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        # Title generation is optional metadata.  Preserve the conversation
        # workflow, but leave an operational trace without logging the user's
        # first message or model response.
        _logger.warning(
            "Agent conversation title generation failed; retaining fallback title "
            "binding_id=%s generation=%s provider_id=%s model=%s "
            "reason=%s error_type=%s",
            binding_id,
            generation,
            provider_id if isinstance(provider_id, str) else None,
            model_name if isinstance(model_name, str) else None,
            _failure_reason(exc),
            type(exc).__name__,
        )
        state = "FALLBACK"

    # A user rename takes the binding row lock and increments title_generation.
    # The guarded update is a compare-and-swap: late output cannot overwrite a
    # manual title. Title metadata is not a user-message activity signal, so
    # preserve the timestamp used by the recent-activity ordering contract.
    if lease_is_current(db, lease):
        db.execute(
            update(AgentConversationBinding)
            .where(
                AgentConversationBinding.id == binding_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
                AgentConversationBinding.title_state == "PENDING",
                AgentConversationBinding.title_generation == generation,
            )
            .values(
                display_title=title,
                title_state=state,
                updated_at=binding.updated_at,
            )
        )
    _redact_task_seed(db, lease, generation)
