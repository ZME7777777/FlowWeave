"""Token-usage attribution without taking ownership of OpenHands state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationUsageBucket,
)
from flowweave.runtime.base import RuntimeUsageSnapshot
from flowweave.shared.database import now

_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _cost(value: object) -> Decimal:
    """Normalize ORM defaults that are not populated until the first flush."""

    return Decimal("0") if value is None else Decimal(str(value))


def _token_count(value: object) -> int:
    """Normalize ORM token defaults that are not populated until the first flush."""

    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        return int(value)
    raise TypeError(f"Unsupported token counter type: {type(value)!r}")


def _summary_integer(value: int | float | str | None) -> int:
    return int(value) if isinstance(value, int | float | str) else 0


def _summary_float(value: int | float | str | None) -> float:
    return float(value) if isinstance(value, int | float | str) else 0.0


def _kind(usage_id: str) -> str:
    if usage_id.startswith("task:"):
        return "SUBAGENT"
    if usage_id in {"condenser", "planning_condenser"}:
        return "CONDENSER"
    if usage_id.startswith("flowweave:"):
        return "PRIMARY"
    return "AUXILIARY"


def _empty() -> dict[str, int | float | str | None]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "accumulated_cost": 0.0,
        "session_count": 0,
        "bucket_count": 0,
        "observed_at": None,
    }


def empty() -> dict[str, int | float | str | None]:
    """Return a DTO-shaped zero total without performing a database read."""

    return _empty()


def _summary(items: Iterable[AgentConversationUsageBucket]) -> dict[str, int | float | str | None]:
    result = _empty()
    bindings: set[str] = set()
    latest = None
    for item in items:
        bindings.add(item.binding_id)
        result["bucket_count"] = _summary_integer(result["bucket_count"]) + 1
        for field in _TOKEN_FIELDS:
            delta = _token_count(getattr(item, f"observed_{field}")) - _token_count(
                getattr(item, f"baseline_{field}")
            )
            result[field] = _summary_integer(result[field]) + delta
        result["accumulated_cost"] = _summary_float(result["accumulated_cost"]) + float(
            _cost(item.observed_cost_usd) - _cost(item.baseline_cost_usd)
        )
        if latest is None or item.observed_at > latest:
            latest = item.observed_at
    result["session_count"] = len(bindings)
    result["total_tokens"] = sum(_summary_integer(result[field]) for field in _TOKEN_FIELDS)
    result["observed_at"] = latest.isoformat() if latest else None
    return result


def capture(
    db: Session, binding: AgentConversationBinding, snapshots: Iterable[RuntimeUsageSnapshot]
) -> dict[str, int | float | str | None]:
    """Store only monotonic source snapshots and return this session's total.

    A Runtime replacement or repeated REST/SSE read returns the same absolute
    OpenHands counters.  Taking a high-water mark makes those operations
    idempotent.  A new binding starts at zero; OpenHands native forks in the
    fixed Runtime explicitly reset metrics, so their history is not charged a
    second time.
    """

    existing = {
        item.usage_id: item
        for item in db.scalars(
            select(AgentConversationUsageBucket).where(
                AgentConversationUsageBucket.binding_id == binding.id
            )
        )
    }
    observed_at = now()
    for source in snapshots:
        item = existing.get(source.usage_id)
        if item is None:
            item = AgentConversationUsageBucket(
                # A Worker reconciles every tenant under bypass. Preserve the
                # binding owner explicitly so the normal user-scoped read
                # projection can see the newly captured bucket.
                owner_user_id=binding.owner_user_id,
                binding_id=binding.id,
                flow_run_id=binding.flow_run_id,
                node_run_id=binding.node_run_id,
                node_attempt_id=binding.node_attempt_id,
                openhands_conversation_id=binding.openhands_conversation_id,
                usage_id=source.usage_id,
                usage_kind=_kind(source.usage_id),
                model_name=source.model_name,
            )
            db.add(item)
            existing[source.usage_id] = item
        item.model_name = source.model_name
        item.usage_kind = _kind(source.usage_id)
        item.observed_cost_usd = float(
            max(_cost(item.observed_cost_usd), _cost(source.accumulated_cost))
        )
        for field in _TOKEN_FIELDS:
            observed = _token_count(getattr(item, f"observed_{field}"))
            source_value = _token_count(getattr(source, field))
            setattr(item, f"observed_{field}", max(observed, source_value))
        item.observed_at = observed_at
    db.flush()
    return _summary(existing.values())


def for_binding(db: Session, binding_id: str) -> dict[str, int | float | str | None]:
    return _summary(
        db.scalars(
            select(AgentConversationUsageBucket).where(
                AgentConversationUsageBucket.binding_id == binding_id
            )
        )
    )


def for_scope(
    db: Session, *, field: str, ids: Iterable[str]
) -> dict[str, dict[str, int | float | str | None]]:
    values = tuple(dict.fromkeys(ids))
    if not values:
        return {}
    column = getattr(AgentConversationUsageBucket, field)
    grouped: dict[str, list[AgentConversationUsageBucket]] = defaultdict(list)
    for item in db.scalars(select(AgentConversationUsageBucket).where(column.in_(values))):
        value = getattr(item, field)
        if value:
            grouped[str(value)].append(item)
    return {value: _summary(grouped[value]) for value in values}


__all__ = ("capture", "empty", "for_binding", "for_scope")
