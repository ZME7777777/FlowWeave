"""Recurring prompt delivery for standalone Agent conversations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkDirectoryVersion
from flowweave.modules.tasks.application.service import enqueue
from flowweave.modules.tasks.infrastructure.models import BackgroundTask
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, conflict, not_found

_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _cron_values(field: str, lower: int, upper: int) -> set[int]:
    values: set[int] = set()
    for item in field.split(","):
        base, separator, step_text = item.partition("/")
        if separator:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError("cron step must be a positive integer")
            step = int(step_text)
        else:
            step = 1
        if base == "*":
            start, end = lower, upper
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError("cron range is invalid")
            start, end = int(start_text), int(end_text)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise ValueError("cron field is invalid")
        if start < lower or end > upper or start > end:
            raise ValueError("cron value is outside its field range")
        values.update(range(start, end + 1, step))
    if lower == 0 and upper == 7 and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def _next_cron_at(expression: str, after: datetime) -> datetime:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            "cron must contain five fields: minute, hour, day-of-month, month and day-of-week"
        )
    minute, hour, day, month, weekday = tuple(
        _cron_values(field, *bounds) for field, bounds in zip(fields, _CRON_BOUNDS, strict=True)
    )
    candidate = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        day_matches = candidate.day in day
        weekday_matches = cron_weekday in weekday
        calendar_matches = (
            day_matches and weekday_matches
            if len(day) != 31 and len(weekday) != 7
            else day_matches or weekday_matches
        )
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.month in month
            and calendar_matches
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron has no matching time in the next year")


def _binding(db: Session, workspace_id: str, binding_id: str) -> AgentConversationBinding:
    binding = db.scalar(
        select(AgentConversationBinding).where(
            AgentConversationBinding.id == binding_id,
            AgentConversationBinding.workspace_id == workspace_id,
            AgentConversationBinding.host_kind == "AGENT_WORKSPACE",
            AgentConversationBinding.lifecycle == "ACTIVE",
        )
    )
    if binding is None:
        raise DomainError("AGENT_SCHEDULE_CONVERSATION_INVALID", "请选择可用的独立 Agent 会话", 409)
    return binding


def _schedule_dict(db: Session, schedule: AgentConversationSchedule) -> dict[str, Any]:
    binding = db.get(AgentConversationBinding, schedule.binding_id)
    return {
        "id": schedule.id,
        "workspace_id": schedule.workspace_id,
        "binding_id": schedule.binding_id,
        "conversation_title": binding.display_title if binding else None,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "cron_expression": schedule.cron_expression,
        "status": schedule.status,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "row_version": schedule.row_version,
        "created_at": schedule.created_at.isoformat(),
        "updated_at": schedule.updated_at.isoformat(),
    }


def _occurrence_dict(db: Session, item: AgentConversationScheduleOccurrence) -> dict[str, Any]:
    binding = db.get(AgentConversationBinding, item.binding_id)
    return {
        "id": item.id,
        "scheduled_for": item.scheduled_for.isoformat() if item.scheduled_for else None,
        "trigger_kind": item.trigger_kind,
        "state": item.state,
        "binding_id": item.binding_id,
        "conversation_title": binding.display_title if binding else None,
        "message_cursor": item.message_cursor,
        "error_detail": item.error_detail,
        "created_at": item.created_at.isoformat(),
    }


def list_schedules(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    query = (
        select(AgentConversationSchedule)
        .where(AgentConversationSchedule.workspace_id == workspace_id)
        .order_by(AgentConversationSchedule.created_at.desc(), AgentConversationSchedule.id.desc())
    )
    return [_schedule_dict(db, item) for item in db.scalars(query)]


def create_schedule(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    binding_id: str,
    prompt: str,
    cron_expression: str,
) -> dict[str, Any]:
    _binding(db, workspace_id, binding_id)
    try:
        next_run_at = _next_cron_at(cron_expression, now())
    except ValueError as exc:
        raise DomainError("AGENT_SCHEDULE_CRON_INVALID", str(exc), 422) from exc
    schedule = AgentConversationSchedule(
        workspace_id=workspace_id,
        binding_id=binding_id,
        name=name,
        prompt=prompt,
        cron_expression=cron_expression,
        next_run_at=next_run_at,
    )
    db.add(schedule)
    finish(db)
    return _schedule_dict(db, schedule)


def update_schedule(
    db: Session,
    workspace_id: str,
    schedule_id: str,
    *,
    expected_row_version: int,
    name: str,
    binding_id: str,
    prompt: str,
    cron_expression: str,
) -> dict[str, Any]:
    schedule = db.scalar(
        select(AgentConversationSchedule)
        .where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    if schedule.row_version != expected_row_version:
        raise conflict(
            "schedule was modified", expected=expected_row_version, actual=schedule.row_version
        )
    _binding(db, workspace_id, binding_id)
    try:
        next_run_at = _next_cron_at(cron_expression, now())
    except ValueError as exc:
        raise DomainError("AGENT_SCHEDULE_CRON_INVALID", str(exc), 422) from exc
    schedule.name = name
    schedule.binding_id = binding_id
    schedule.prompt = prompt
    schedule.cron_expression = cron_expression
    schedule.row_version += 1
    if schedule.status == "ACTIVE":
        schedule.next_run_at = next_run_at
    finish(db)
    return _schedule_dict(db, schedule)


def set_schedule_state(
    db: Session,
    workspace_id: str,
    schedule_id: str,
    *,
    expected_row_version: int,
    status: str,
) -> dict[str, Any]:
    schedule = db.scalar(
        select(AgentConversationSchedule)
        .where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    if schedule.row_version != expected_row_version:
        raise conflict(
            "schedule was modified", expected=expected_row_version, actual=schedule.row_version
        )
    schedule.status = status
    schedule.row_version += 1
    schedule.next_run_at = (
        _next_cron_at(schedule.cron_expression, now()) if status == "ACTIVE" else None
    )
    finish(db)
    return _schedule_dict(db, schedule)


def _enqueue_occurrence(
    db: Session,
    schedule: AgentConversationSchedule,
    *,
    trigger_kind: str,
    scheduled_for: datetime | None,
) -> AgentConversationScheduleOccurrence:
    occurrence = AgentConversationScheduleOccurrence(
        schedule_id=schedule.id,
        binding_id=str(uuid4()),
        scheduled_for=scheduled_for,
        trigger_kind=trigger_kind,
        state="PENDING",
    )
    db.add(occurrence)
    db.flush()
    enqueue(
        db,
        task_type="DELIVER_AGENT_CONVERSATION_SCHEDULE",
        aggregate_type="AGENT_CONVERSATION_SCHEDULE_OCCURRENCE",
        aggregate_id=occurrence.id,
        idempotency_key=f"deliver-agent-conversation-schedule:{occurrence.id}",
    )
    return occurrence


def trigger_schedule(db: Session, workspace_id: str, schedule_id: str) -> dict[str, Any]:
    schedule = db.scalar(
        select(AgentConversationSchedule)
        .where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    _enqueue_occurrence(db, schedule, trigger_kind="MANUAL", scheduled_for=None)
    finish(db)
    return _schedule_dict(db, schedule)


def delete_schedule(db: Session, workspace_id: str, schedule_id: str) -> None:
    schedule = db.scalar(
        select(AgentConversationSchedule)
        .where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    occurrence_ids = select(AgentConversationScheduleOccurrence.id).where(
        AgentConversationScheduleOccurrence.schedule_id == schedule.id
    )
    db.execute(
        delete(BackgroundTask).where(
            BackgroundTask.aggregate_type == "AGENT_CONVERSATION_SCHEDULE_OCCURRENCE",
            BackgroundTask.aggregate_id.in_(occurrence_ids),
        )
    )
    db.execute(
        delete(AgentConversationScheduleOccurrence).where(
            AgentConversationScheduleOccurrence.schedule_id == schedule.id
        )
    )
    db.delete(schedule)
    finish(db)


def list_occurrences(db: Session, workspace_id: str, schedule_id: str) -> list[dict[str, Any]]:
    schedule = db.scalar(
        select(AgentConversationSchedule).where(
            AgentConversationSchedule.id == schedule_id,
            AgentConversationSchedule.workspace_id == workspace_id,
        )
    )
    if schedule is None:
        raise not_found("agent_conversation_schedule", schedule_id)
    return [
        _occurrence_dict(db, item)
        for item in db.scalars(
            select(AgentConversationScheduleOccurrence)
            .where(AgentConversationScheduleOccurrence.schedule_id == schedule.id)
            .order_by(AgentConversationScheduleOccurrence.created_at.desc())
            .limit(30)
        )
    ]


def scan_due_schedules(db: Session) -> int:
    current = now()
    schedules = list(
        db.scalars(
            select(AgentConversationSchedule)
            .where(
                AgentConversationSchedule.status == "ACTIVE",
                AgentConversationSchedule.next_run_at.is_not(None),
                AgentConversationSchedule.next_run_at <= current,
            )
            .order_by(AgentConversationSchedule.next_run_at, AgentConversationSchedule.id)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    )
    for schedule in schedules:
        due_at = schedule.next_run_at
        with tenant_user(schedule.owner_user_id):
            _enqueue_occurrence(db, schedule, trigger_kind="SCHEDULED", scheduled_for=due_at)
        schedule.next_run_at = _next_cron_at(schedule.cron_expression, current)
        schedule.row_version += 1
    db.flush()
    return len(schedules)


def _run_binding_id(
    schedule: AgentConversationSchedule, occurrence: AgentConversationScheduleOccurrence
) -> str:
    return f"schedule:{schedule.id}:{occurrence.id}"


def _copy_source_capabilities(db: Session, source_binding_id: str) -> tuple[str, ...]:
    source_capabilities = list(
        db.scalars(
            select(AgentConversationCapability)
            .where(AgentConversationCapability.binding_id == source_binding_id)
            .order_by(AgentConversationCapability.position)
        )
    )
    return tuple(item.capability_version_id for item in source_capabilities)


def _work_directory_id(db: Session, binding: AgentConversationBinding) -> str | None:
    if binding.work_directory_version_id is None:
        return None
    return db.scalar(
        select(AgentWorkDirectoryVersion.work_directory_id).where(
            AgentWorkDirectoryVersion.id == binding.work_directory_version_id
        )
    )


def deliver_occurrence(db: Session, occurrence_id: str) -> None:
    occurrence = db.scalar(
        select(AgentConversationScheduleOccurrence)
        .where(AgentConversationScheduleOccurrence.id == occurrence_id)
        .with_for_update()
    )
    if occurrence is None or occurrence.state in {"STARTED", "FAILED"}:
        return
    schedule = db.get(AgentConversationSchedule, occurrence.schedule_id)
    if schedule is None:
        occurrence.state = "FAILED"
        occurrence.error_detail = "schedule was deleted before delivery"
        db.flush()
        return
    source = _binding(db, schedule.workspace_id, schedule.binding_id)
    try:
        if source.model_provider_id is None:
            raise DomainError("AGENT_SCHEDULE_MODEL_MISSING", "定时任务来源会话未配置模型", 409)
        created = conversations.bootstrap_conversation(
            db,
            schedule.workspace_id,
            work_directory_id=_work_directory_id(db, source),
            model_provider_id=source.model_provider_id,
            model_name=source.model_name,
            reasoning_effort=source.reasoning_effort,
            fallback_models=tuple(source.fallback_models_json),
            content=schedule.prompt,
            capability_version_ids=_copy_source_capabilities(db, source.id),
            conversation_id=occurrence.binding_id,
            idempotency_key=_run_binding_id(schedule, occurrence),
        )
        run_binding = created["conversation"]
        occurrence.binding_id = str(run_binding["id"])
        occurrence.state = "STARTED"
        occurrence.message_cursor = created.get("cursor")
        schedule.last_run_at = now()
    except DomainError as exc:
        occurrence.state = "FAILED"
        occurrence.error_detail = f"{exc.code}: {exc.message}"
    db.flush()
