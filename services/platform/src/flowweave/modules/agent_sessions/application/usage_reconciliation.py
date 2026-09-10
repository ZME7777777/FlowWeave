"""Worker-side, read-only reconciliation of formal OpenHands usage counters.

OpenHands owns the accumulated counters. This module only schedules bounded
reads and feeds their snapshots through the existing high-water projection, so
retries, Runtime replacement, and repeated reads cannot double count tokens or
cost.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import usage as usage_projection
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeEventBatch, RuntimeHandle
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

_AGENT_WORKSPACE = "AGENT_WORKSPACE"
_FLOW_NODE = "FLOW_NODE"


def claim_due_bindings(db: Session, *, interval_seconds: int, limit: int) -> tuple[str, ...]:
    """Claim a small due page without holding a SQL transaction over Runtime I/O.

    ``usage_reconcile_after`` is advanced before the OpenHands request. A
    failed request is explicitly rescheduled by :func:`record_failure`; a
    crashed Worker leaves the bounded future timestamp as the only harmless
    delay. ``SKIP LOCKED`` makes concurrent Workers divide the work.
    """

    current = now()
    bindings = list(
        db.scalars(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.lifecycle == "ACTIVE",
                or_(
                    AgentConversationBinding.usage_reconcile_after.is_(None),
                    AgentConversationBinding.usage_reconcile_after <= current,
                ),
            )
            .order_by(
                AgentConversationBinding.usage_reconcile_after,
                AgentConversationBinding.created_at,
                AgentConversationBinding.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    next_due = current + timedelta(seconds=interval_seconds)
    for binding in bindings:
        binding.usage_reconcile_after = next_due
    db.flush()
    return tuple(binding.id for binding in bindings)


def resolve_handle(db: Session, binding_id: str) -> RuntimeHandle:
    """Resolve the current authorized Runtime route for one claimed binding."""

    binding = db.get(AgentConversationBinding, binding_id)
    if binding is None or binding.lifecycle != "ACTIVE":
        raise DomainError(
            "USAGE_RECONCILIATION_BINDING_UNAVAILABLE",
            "The Conversation is no longer active for usage reconciliation",
            409,
        )
    if binding.host_kind == _AGENT_WORKSPACE:
        # Keep host-specific Runtime routing inside its existing application
        # module; this reconciler never guesses containers or endpoints.
        from flowweave.modules.agent_sessions.application import conversations

        # Direct Agent workspaces derive their Runtime project path from the
        # effective tenant. The Worker normally runs under bypass, so restore
        # the binding owner while resolving this read-only handle.
        with tenant_user(binding.owner_user_id):
            return conversations.usage_reconciliation_handle(db, binding)
    if binding.host_kind == _FLOW_NODE:
        from flowweave.modules.agent_sessions.application import flow_node_conversations

        return flow_node_conversations.usage_reconciliation_handle(db, binding)
    raise DomainError(
        "USAGE_RECONCILIATION_BINDING_INVALID",
        "The Conversation host does not support usage reconciliation",
        409,
    )


def record_success(db: Session, binding_id: str, batch: RuntimeEventBatch) -> bool:
    """Persist one formal snapshot through the existing monotonic projector."""

    binding = db.get(AgentConversationBinding, binding_id)
    if binding is None or binding.lifecycle != "ACTIVE":
        return False
    usage_projection.capture(db, binding, batch.usage)
    binding.usage_reconciled_at = now()
    db.flush()
    return True


def record_failure(db: Session, binding_id: str, *, retry_seconds: int) -> None:
    """Make a transient Runtime/read failure eligible for a quick retry."""

    binding = db.get(AgentConversationBinding, binding_id)
    if binding is not None and binding.lifecycle == "ACTIVE":
        binding.usage_reconcile_after = now() + timedelta(seconds=retry_seconds)
        db.flush()


__all__ = (
    "claim_due_bindings",
    "record_failure",
    "record_success",
    "resolve_handle",
)
