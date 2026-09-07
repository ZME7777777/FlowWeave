"""Compatibility boundary for retired FlowWeave Task recovery automation.

OpenHands owns Task/child-Agent execution. FlowWeave only reads its formal
events; it must not manufacture timeouts, interrupts, Runtime replacement, or
parent Conversation continuation from those observations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.runtime.base import RuntimeEvent, RuntimeHandle


def task_control_projection(_db: Session, _binding_id: str) -> list[dict[str, Any]]:
    """Keep the read response compatible without exposing retired controls."""

    return []


def observe_task_watchdogs(
    _db: Session,
    _binding: AgentConversationBinding,
    _events: tuple[RuntimeEvent, ...],
) -> None:
    """Observe no control state; monitoring is projected directly from events."""


def observe_task_watchdogs_from_runtime(
    _db: Session,
    _binding: AgentConversationBinding,
    _handle: RuntimeHandle,
) -> None:
    """Compatibility no-op for message delivery paths."""


__all__ = (
    "observe_task_watchdogs",
    "observe_task_watchdogs_from_runtime",
    "task_control_projection",
)
