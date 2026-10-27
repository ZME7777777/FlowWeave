"""Bounded, user-isolated record directory without execution-detail hydration."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from flowweave.shared.domain.enums import NodeRunState
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.models import FlowRun, FlowRunSchedule, NodeRun, RunEvent


def list_record_directory(
    db: Session,
    parent_run_id: str,
    *,
    kind: Literal["ALL", "MANUAL", "AUTOMATIC"] = "ALL",
    cursor: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    # The normal user-scoped session/RLS applies to parent and child reads alike.
    if db.get(FlowRun, parent_run_id) is None:
        raise not_found("flow_run", parent_run_id)
    if kind not in {"ALL", "MANUAL", "AUTOMATIC"} or not 1 <= limit <= 100:
        raise DomainError("INVALID_RECORD_PAGE", "invalid record directory page", 400)
    filters = [
        FlowRun.parent_flow_run_id == parent_run_id,
        or_(
            FlowRun.run_mode != "AUTOMATIC",
            ~select(RunEvent.cursor)
            .where(
                RunEvent.flow_run_id == FlowRun.id,
                RunEvent.event_type == "AUTOMATIC_RUN_DELETE_REQUESTED",
            )
            .exists(),
        ),
    ]
    if kind != "ALL":
        filters.append(FlowRun.run_mode == kind)
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError("oversize cursor")
            scope, filter_kind, timestamp, record_id = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            when = datetime.fromisoformat(timestamp)
            if (
                scope != parent_run_id
                or filter_kind != kind
                or when.tzinfo is None
                or not isinstance(record_id, str)
                or not record_id
            ):
                raise ValueError("cursor scope mismatch")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DomainError(
                "INVALID_RECORD_CURSOR", "invalid record directory cursor", 400
            ) from exc
        filters.append(
            or_(FlowRun.started_at < when, and_(FlowRun.started_at == when, FlowRun.id < record_id))
        )
    # Select only directory fields, never drafts, snapshots, attempts, artifacts,
    # usage or Runtime projections. Even plan JSON is projected by key in SQL.
    rows = list(
        db.execute(
            select(
                FlowRun.id,
                FlowRun.name,
                FlowRun.run_mode,
                FlowRun.state,
                FlowRun.started_at,
                FlowRun.schedule_id,
                FlowRunSchedule.name.label("schedule_name"),
                FlowRun.automation_plan_json["start_node_key"].as_string().label("start_node_key"),
                FlowRun.automation_plan_json["reachable_node_keys"].label("reachable_node_keys"),
            )
            .outerjoin(FlowRunSchedule, FlowRun.schedule_id == FlowRunSchedule.id)
            .where(*filters)
            .order_by(FlowRun.started_at.desc(), FlowRun.id.desc())
            .limit(limit + 1)
        ).mappings()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    counts = (
        {
            run_id: (int(total), int(accepted))
            for run_id, total, accepted in db.execute(
                select(
                    NodeRun.flow_run_id,
                    func.count(NodeRun.id),
                    func.sum(case((NodeRun.state == NodeRunState.ACCEPTED, 1), else_=0)),
                )
                .where(NodeRun.flow_run_id.in_([row["id"] for row in rows]))
                .group_by(NodeRun.flow_run_id)
            )
        }
        if rows
        else {}
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        automatic = row["run_mode"] == "AUTOMATIC"
        keys = row["reachable_node_keys"]
        accepted_count = counts.get(row["id"], (0, 0))[1]
        reachable_count = len(cast(list[Any], keys)) if isinstance(keys, list) else 0
        detail = (
            f"{accepted_count}/{reachable_count} 节点完成"
            if automatic
            else "逐步配置与确认执行"
            if row["start_node_key"] or counts.get(row["id"], (0, 0))[0]
            else "等待配置起始节点"
        )
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "kind": row["run_mode"],
                "state": row["state"],
                "started_at": row["started_at"].isoformat(),
                "detail": detail,
                "schedule_id": row["schedule_id"],
                "schedule_name": row["schedule_name"],
            }
        )
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [
                        parent_run_id,
                        kind,
                        last["started_at"].isoformat(),
                        last["id"],
                    ]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {"items": items, "next_cursor": next_cursor}
