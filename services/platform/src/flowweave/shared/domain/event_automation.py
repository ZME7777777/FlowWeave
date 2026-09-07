"""Governed event-trigger primitives for FlowRun runtime enhancements.

This module is deliberately framework free.  It describes the contract consumed
by a future durable trigger dispatcher; it does not deliver network requests or
mutate an Attempt.  Runtime event identity is copied from OpenHands' formal
fields so a dispatcher can be idempotent without guessing event order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class FailureClass(StrEnum):
    """Failure categories used by trigger filters and recovery policy."""

    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    TRANSIENT_TIMEOUT = "TRANSIENT_TIMEOUT"
    TRANSIENT_UNAVAILABLE = "TRANSIENT_UNAVAILABLE"
    AUTHENTICATION = "AUTHENTICATION"
    QUOTA = "QUOTA"
    CONTENT_POLICY = "CONTENT_POLICY"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN = "UNKNOWN"


class EventSource(StrEnum):
    OPENHANDS = "OPENHANDS"
    RUNTIME = "RUNTIME"
    ORCHESTRATION = "ORCHESTRATION"


class ActionType(StrEnum):
    """Platform-owned actions; values are not arbitrary Runtime commands."""

    RESUME_CONVERSATION = "RESUME_CONVERSATION"
    WEBHOOK = "WEBHOOK"
    NOTIFY = "NOTIFY"
    CREATE_TASK = "CREATE_TASK"
    PAUSE_ATTEMPT = "PAUSE_ATTEMPT"
    HANDOFF_HUMAN = "HANDOFF_HUMAN"


_TRANSIENT_ERROR_TERMS: dict[FailureClass, tuple[str, ...]] = {
    FailureClass.TRANSIENT_NETWORK: (
        "connection",
        "connecterror",
        "network",
        "broken pipe",
        "connection reset",
    ),
    FailureClass.TRANSIENT_TIMEOUT: ("timeout", "timed out", "deadline exceeded"),
    FailureClass.TRANSIENT_UNAVAILABLE: (
        "temporarily unavailable",
        "service unavailable",
        "server overloaded",
        "bad gateway",
        "gateway timeout",
    ),
}
_NON_TRANSIENT_ERROR_TERMS: dict[FailureClass, tuple[str, ...]] = {
    FailureClass.AUTHENTICATION: ("unauthorized", "authentication", "invalid api key", "401"),
    FailureClass.QUOTA: ("quota", "rate limit", "too many requests", "429"),
    FailureClass.CONTENT_POLICY: ("content policy", "safety policy", "policy violation"),
    FailureClass.CONTEXT_LIMIT: ("context length", "context window", "too many tokens"),
    FailureClass.INVALID_REQUEST: ("invalid request", "bad request", "validation error", "400"),
}


def _empty_payload() -> dict[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """Stable, redacted event envelope exposed to a trigger dispatcher."""

    event_id: str
    event_type: str
    source: EventSource
    flow_run_id: str
    node_run_id: str | None = None
    attempt_id: str | None = None
    conversation_id: str | None = None
    parent_id: str | None = None
    action_id: str | None = None
    tool_call_id: str | None = None
    failure_class: FailureClass | None = None
    payload: dict[str, Any] = field(default_factory=_empty_payload, repr=False)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("trigger event_id cannot be blank")
        if not self.event_type.strip():
            raise ValueError("trigger event_type cannot be blank")
        if not self.flow_run_id.strip():
            raise ValueError("trigger flow_run_id cannot be blank")


@dataclass(frozen=True, slots=True)
class TriggerFilter:
    """Conjunctive filter; an empty field means any value."""

    event_types: frozenset[str] = frozenset()
    sources: frozenset[EventSource] = frozenset()
    failure_classes: frozenset[FailureClass] = frozenset()
    flow_run_ids: frozenset[str] = frozenset()
    node_run_ids: frozenset[str] = frozenset()

    def matches(self, event: TriggerEvent) -> bool:
        return (
            (not self.event_types or event.event_type in self.event_types)
            and (not self.sources or event.source in self.sources)
            and (not self.failure_classes or event.failure_class in self.failure_classes)
            and (not self.flow_run_ids or event.flow_run_id in self.flow_run_ids)
            and (not self.node_run_ids or event.node_run_id in self.node_run_ids)
        )


@dataclass(frozen=True, slots=True)
class TriggerAction:
    """One governed action to enqueue after a trigger match."""

    action_type: ActionType
    config: dict[str, Any] = field(default_factory=_empty_payload, repr=False)


@dataclass(frozen=True, slots=True)
class EventTrigger:
    """Versioned trigger definition with an ordered action chain."""

    trigger_id: str
    version: int
    event_filter: TriggerFilter
    actions: tuple[TriggerAction, ...]
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.trigger_id.strip():
            raise ValueError("trigger_id cannot be blank")
        if self.version < 1:
            raise ValueError("trigger version must be positive")
        if not self.actions:
            raise ValueError("trigger must contain at least one action")

    def matches(self, event: TriggerEvent) -> bool:
        return self.enabled and self.event_filter.matches(event)


def classify_failure(error_code: str | None, message: str | None) -> FailureClass:
    """Classify a failure conservatively; unknown errors are never auto-retried."""

    haystack = f"{error_code or ''} {message or ''}".casefold()
    for failure_class, terms in _NON_TRANSIENT_ERROR_TERMS.items():
        if any(term in haystack for term in terms):
            return failure_class
    for failure_class, terms in _TRANSIENT_ERROR_TERMS.items():
        if any(term in haystack for term in terms):
            return failure_class
    return FailureClass.UNKNOWN


def is_auto_recovery_eligible(failure_class: FailureClass | None) -> bool:
    """Only transient infrastructure failures may feed resume automation."""

    return failure_class in {
        FailureClass.TRANSIENT_NETWORK,
        FailureClass.TRANSIENT_TIMEOUT,
        FailureClass.TRANSIENT_UNAVAILABLE,
    }


def action_idempotency_key(trigger: EventTrigger, event: TriggerEvent, action_index: int) -> str:
    """Return a stable key for an outbox row, independent of delivery retries."""

    if action_index < 0 or action_index >= len(trigger.actions):
        raise IndexError("action index is outside the trigger action chain")
    body = json.dumps(
        {
            "trigger_id": trigger.trigger_id,
            "version": trigger.version,
            "event_id": event.event_id,
            "action_index": action_index,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


StateDrivingEvent = Literal[
    "ERROR",
    "COMPLETED",
    "HUMAN_INPUT_REQUIRED",
    "ATTEMPT_STATE_CHANGED",
    "TASK_STATE_CHANGED",
]

STATE_DRIVING_EVENT_TYPES: frozenset[StateDrivingEvent] = frozenset(
    {
        "ERROR",
        "COMPLETED",
        "HUMAN_INPUT_REQUIRED",
        "ATTEMPT_STATE_CHANGED",
        "TASK_STATE_CHANGED",
    }
)

OBSERVATION_EVENT_TYPES = frozenset(
    {
        "MESSAGE",
        "TOOL",
        "TOOL_CALL",
        "TOOL_RESULT",
        "THOUGHT",
        "STATE",
        "OUTPUT",
        "CONDENSATION_REQUESTED",
        "CONDENSATION_COMPLETED",
    }
)


__all__ = (
    "ActionType",
    "EventSource",
    "EventTrigger",
    "FailureClass",
    "OBSERVATION_EVENT_TYPES",
    "STATE_DRIVING_EVENT_TYPES",
    "TriggerAction",
    "TriggerEvent",
    "TriggerFilter",
    "action_idempotency_key",
    "classify_failure",
    "is_auto_recovery_eligible",
)
