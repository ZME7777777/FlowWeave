"""User-scoped, server-paged unique run conversations for an Agent schedule."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.schedules import occurrence_dict
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.shared.errors import DomainError, not_found


def list_run_conversations(
    db: Session, workspace_id: str, schedule_id: str, *, cursor: str | None = None, limit: int = 20
) -> dict[str, Any]:
    schedule = db.scalar(
        select(AgentConversationSchedule).where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    if not 1 <= limit <= 100:
        raise DomainError("INVALID_SCHEDULE_PAGE", "invalid schedule history page", 400)
    occurrence = AgentConversationScheduleOccurrence
    # Prefer a readable delivery of a continuing conversation over a newer
    # pending/failed delivery. Order each conversation by its latest trigger.
    ranked = (
        select(
            occurrence.id,
            occurrence.binding_id,
            func.max(occurrence.created_at)
            .over(partition_by=occurrence.binding_id)
            .label("latest_at"),
            func.first_value(occurrence.trigger_kind)
            .over(
                partition_by=occurrence.binding_id,
                order_by=(occurrence.created_at.desc(), occurrence.id.desc()),
            )
            .label("latest_trigger"),
            func.row_number()
            .over(
                partition_by=occurrence.binding_id,
                order_by=(
                    case((occurrence.state == "STARTED", 0), else_=1),
                    occurrence.created_at.desc(),
                    occurrence.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(occurrence.schedule_id == schedule_id)
        .subquery()
    )
    filters = [ranked.c.rank == 1]
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError("oversize cursor")
            scope, owner, timestamp, binding_id = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            when = datetime.fromisoformat(timestamp)
            if (
                scope != workspace_id
                or owner != schedule_id
                or when.tzinfo is None
                or not isinstance(binding_id, str)
                or not binding_id
            ):
                raise ValueError("cursor scope mismatch")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DomainError(
                "INVALID_SCHEDULE_CURSOR", "invalid schedule history cursor", 400
            ) from exc
        filters.append(
            or_(
                ranked.c.latest_at < when,
                and_(ranked.c.latest_at == when, ranked.c.binding_id < binding_id),
            )
        )
    total = (
        db.scalar(
            select(func.count(func.distinct(occurrence.binding_id))).where(
                occurrence.schedule_id == schedule_id
            )
        )
        or 0
    )
    rows = list(
        db.execute(
            select(occurrence, ranked.c.latest_at, ranked.c.latest_trigger)
            .join(ranked, occurrence.id == ranked.c.id)
            .where(*filters)
            .order_by(ranked.c.latest_at.desc(), ranked.c.binding_id.desc())
            .limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    bindings = (
        {
            item.id: item
            for item in db.scalars(
                select(AgentConversationBinding).where(
                    AgentConversationBinding.workspace_id == workspace_id,
                    AgentConversationBinding.id.in_([row[0].binding_id for row in rows]),
                )
            )
        }
        if rows
        else {}
    )
    items = [
        {
            **occurrence_dict(db, item, bindings=bindings),
            "latest_run_at": latest.isoformat(),
            "latest_trigger_kind": latest_trigger,
        }
        for item, latest, latest_trigger in rows
    ]
    next_cursor = None
    if more:
        item, latest, _ = rows[-1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [
                        workspace_id,
                        schedule_id,
                        latest.isoformat(),
                        item.binding_id,
                    ]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {"items": items, "next_cursor": next_cursor, "total": total}
