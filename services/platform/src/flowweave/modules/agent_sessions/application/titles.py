"""One-shot, FlowWeave-only titles for lazy Agent Conversation bootstrap."""

from __future__ import annotations

import re
from typing import Any, cast

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from flowweave.modules.agent_workspaces.infrastructure.models import AgentConversationBinding
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


def _clean_title(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split()).strip(" '“”‘’\"")
    cleaned = cleaned[:80]
    if not cleaned or _MECHANICAL_TITLE.fullmatch(cleaned):
        return fallback
    return cleaned


def _chat_title(snapshot: TitleProviderSnapshot, first_message: str) -> str:
    payload = {
        "model": snapshot.model,
        "stream": False,
        "temperature": 0,
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
        "stream": False,
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
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(
            f"{snapshot.base_url}/responses", headers=snapshot.headers, json=payload
        )
        response.raise_for_status()
    body = cast(dict[str, Any], response.json())
    direct = body.get("output_text")
    if isinstance(direct, str):
        return direct
    output = body.get("output")
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
    try:
        provider_id = payload.get("model_provider_id")
        model_name = payload.get("model_name")
        if not isinstance(provider_id, str) or not isinstance(model_name, str):
            raise ValueError("title provider metadata is invalid")
        title = _clean_title(
            generate_title(title_provider_snapshot(db, provider_id, model_name), first_message),
            fallback,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        state = "FALLBACK"

    # A user rename takes the binding row lock and increments title_generation.
    # This guarded update is therefore a compare-and-swap: late model output
    # simply becomes a no-op and can never overwrite a manual title.
    if lease_is_current(db, lease):
        db.execute(
            update(AgentConversationBinding)
            .where(
                AgentConversationBinding.id == binding_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
                AgentConversationBinding.title_state == "PENDING",
                AgentConversationBinding.title_generation == generation,
            )
            .values(display_title=title, title_state=state)
        )
    _redact_task_seed(db, lease, generation)
