"""Recurring prompt delivery for standalone Agent conversations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkDirectory
from flowweave.modules.credentials.application.service import resolve_credentials_for_agent
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
    candidate = after.astimezone(ZoneInfo("Asia/Shanghai")).replace(
        second=0, microsecond=0
    ) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        day_matches = candidate.day in day
        weekday_matches = cron_weekday in weekday
        calendar_matches = (
            day_matches and weekday_matches
            if len(day) == 31 or len(weekday) == 7
            else day_matches or weekday_matches
        )
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.month in month
            and calendar_matches
        ):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError("cron has no matching time in the next year")


def _schedule_binding(
    db: Session, schedule: AgentConversationSchedule
) -> AgentConversationBinding | None:
    return db.get(AgentConversationBinding, schedule.binding_id) if schedule.binding_id else None


def _work_directory(db: Session, workspace_id: str, work_directory_id: str) -> AgentWorkDirectory:
    item = db.scalar(
        select(AgentWorkDirectory).where(
            AgentWorkDirectory.id == work_directory_id,
            AgentWorkDirectory.workspace_id == workspace_id,
        )
    )
    if item is None:
        raise DomainError("AGENT_SCHEDULE_WORKSPACE_INVALID", "请选择可用的工作区", 409)
    return item


def _normalized_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _validated_template_configuration(
    db: Session, capability_version_ids: tuple[str, ...], credential_ids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    capabilities = _normalized_ids(capability_version_ids)
    # Resolve through the same creation contract used by native conversations.
    conversations._validated_capabilities(
        db, capabilities, allowed_types=conversations._CREATION_CAPABILITY_TYPES
    )
    credentials = _normalized_ids(credential_ids)
    resolve_credentials_for_agent(db, credentials)
    return capabilities, credentials


def _schedule_dict(db: Session, schedule: AgentConversationSchedule) -> dict[str, Any]:
    binding = _schedule_binding(db, schedule)
    directory = (
        db.get(AgentWorkDirectory, schedule.work_directory_id)
        if schedule.work_directory_id
        else None
    )
    return {
        "id": schedule.id,
        "workspace_id": schedule.workspace_id,
        "binding_id": schedule.binding_id,
        "conversation_title": binding.display_title if binding else None,
        "work_directory_id": schedule.work_directory_id,
        "work_directory_name": directory.display_name if directory else None,
        "workspace_mode": schedule.workspace_mode,
        "conversation_mode": schedule.conversation_mode,
        "model_provider_id": schedule.model_provider_id,
        "model_name": schedule.model_name,
        "reasoning_effort": schedule.reasoning_effort,
        "capability_version_ids": list(schedule.capability_version_ids_json or []),
        "credential_ids": list(schedule.credential_ids_json or []),
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


def _automation_prefix(schedule: AgentConversationSchedule) -> str:
    last_run = schedule.last_run_at.isoformat() if schedule.last_run_at else "never"
    metadata_root = (
        "." if schedule.workspace_mode == "AUTOMATION" else f".flowweave/automations/{schedule.id}"
    )
    config_path = "automation.toml" if metadata_root == "." else f"{metadata_root}/automation.toml"
    memory_path = "memory.md" if metadata_root == "." else f"{metadata_root}/memory.md"
    return (
        f"Automation: {schedule.name}\n"
        f"Automation ID: {schedule.id}\n"
        f"Automation config: {config_path}\n"
        f"Automation memory: {memory_path}\n"
        f"Last run: {last_run}\n\n"
        f"仅在当前自动化工作区中工作。先读取 {config_path} 和 {memory_path}；"
        f"完成后更新 {memory_path}。所有新建文件、报告和日志必须写入当前工作区。\n\n"
    )


def _automation_prompt(schedule: AgentConversationSchedule) -> str:
    return _automation_prefix(schedule) + schedule.prompt


def _write_automation_files(db: Session, schedule: AgentConversationSchedule) -> None:
    if not schedule.work_directory_id or not schedule.model_provider_id or not schedule.model_name:
        raise DomainError("AGENT_SCHEDULE_CONFIGURATION_INCOMPLETE", "定时任务配置不完整", 409)
    work_directories.write_automation_files(
        db,
        schedule.workspace_id,
        schedule.work_directory_id,
        automation_id=schedule.id,
        name=schedule.name,
        prompt=schedule.prompt,
        cron_expression=schedule.cron_expression,
        workspace_mode=schedule.workspace_mode,
        conversation_mode=schedule.conversation_mode,
        model_provider_id=schedule.model_provider_id,
        model_name=schedule.model_name,
        reasoning_effort=schedule.reasoning_effort,
        capability_version_ids=tuple(schedule.capability_version_ids_json or []),
        credential_ids=tuple(schedule.credential_ids_json or []),
    )


def create_schedule(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    prompt: str,
    cron_expression: str,
    workspace_mode: str,
    work_directory_id: str | None,
    conversation_mode: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
    capability_version_ids: tuple[str, ...],
    credential_ids: tuple[str, ...],
) -> dict[str, Any]:
    capabilities, credentials = _validated_template_configuration(
        db, capability_version_ids, credential_ids
    )
    try:
        next_run_at = _next_cron_at(cron_expression, now())
    except ValueError as exc:
        raise DomainError("AGENT_SCHEDULE_CRON_INVALID", str(exc), 422) from exc
    if workspace_mode == "EXISTING":
        if not work_directory_id:
            raise DomainError("AGENT_SCHEDULE_WORKSPACE_REQUIRED", "请选择已有工作区", 422)
        _work_directory(db, workspace_id, work_directory_id)
    elif workspace_mode != "AUTOMATION":
        raise DomainError("AGENT_SCHEDULE_WORKSPACE_MODE_INVALID", "工作区模式无效", 422)
    if conversation_mode not in {"NEW", "CONTINUE"}:
        raise DomainError("AGENT_SCHEDULE_CONVERSATION_MODE_INVALID", "会话模式无效", 422)
    schedule = AgentConversationSchedule(
        workspace_id=workspace_id,
        work_directory_id=work_directory_id,
        workspace_mode=workspace_mode,
        conversation_mode=conversation_mode,
        model_provider_id=model_provider_id,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        capability_version_ids_json=list(capabilities),
        credential_ids_json=list(credentials),
        name=name,
        prompt=prompt,
        cron_expression=cron_expression,
        next_run_at=next_run_at,
    )
    db.add(schedule)
    db.flush()
    if schedule.workspace_mode == "AUTOMATION":
        directory = work_directories.create_automation_work_directory(
            db, workspace_id, schedule.id, f"自动化 · {schedule.name}"
        )
        schedule.work_directory_id = str(directory["id"])
    _write_automation_files(db, schedule)
    finish(db)
    return _schedule_dict(db, schedule)


def update_schedule(
    db: Session,
    workspace_id: str,
    schedule_id: str,
    *,
    expected_row_version: int,
    name: str,
    prompt: str,
    cron_expression: str,
    conversation_mode: str,
    model_provider_id: str,
    model_name: str,
    reasoning_effort: str | None,
    capability_version_ids: tuple[str, ...],
    credential_ids: tuple[str, ...],
) -> dict[str, Any]:
    capabilities, credentials = _validated_template_configuration(
        db, capability_version_ids, credential_ids
    )
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
    if conversation_mode not in {"NEW", "CONTINUE"}:
        raise DomainError("AGENT_SCHEDULE_CONVERSATION_MODE_INVALID", "会话模式无效", 422)
    try:
        next_run_at = _next_cron_at(cron_expression, now())
    except ValueError as exc:
        raise DomainError("AGENT_SCHEDULE_CRON_INVALID", str(exc), 422) from exc
    schedule.name = name
    schedule.prompt = prompt
    schedule.cron_expression = cron_expression
    schedule.conversation_mode = conversation_mode
    schedule.model_provider_id = model_provider_id
    schedule.model_name = model_name
    schedule.reasoning_effort = reasoning_effort
    schedule.capability_version_ids_json = list(capabilities)
    schedule.credential_ids_json = list(credentials)
    if conversation_mode == "NEW":
        schedule.binding_id = None
    schedule.row_version += 1
    if schedule.status == "ACTIVE":
        schedule.next_run_at = next_run_at
    _write_automation_files(db, schedule)
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


def _bootstrap_run(
    db: Session,
    schedule: AgentConversationSchedule,
    occurrence: AgentConversationScheduleOccurrence,
) -> dict[str, Any]:
    if not schedule.work_directory_id:
        raise DomainError("AGENT_SCHEDULE_WORKSPACE_MISSING", "定时任务工作区不可用", 409)
    if not schedule.model_provider_id or not schedule.model_name:
        raise DomainError("AGENT_SCHEDULE_MODEL_MISSING", "定时任务未配置模型", 409)
    return conversations.bootstrap_conversation(
        db,
        schedule.workspace_id,
        work_directory_id=schedule.work_directory_id,
        model_provider_id=schedule.model_provider_id,
        model_name=schedule.model_name,
        reasoning_effort=schedule.reasoning_effort,
        content=_automation_prompt(schedule),
        capability_version_ids=tuple(schedule.capability_version_ids_json or []),
        credential_ids=tuple(schedule.credential_ids_json or []),
        conversation_id=occurrence.binding_id,
        idempotency_key=_run_binding_id(schedule, occurrence),
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
    try:
        if schedule.conversation_mode == "CONTINUE" and schedule.binding_id:
            delivered = conversations.message(
                db, schedule.workspace_id, schedule.binding_id, _automation_prompt(schedule)
            )
            occurrence.binding_id = schedule.binding_id
            occurrence.message_cursor = delivered.get("cursor")
        else:
            created = _bootstrap_run(db, schedule, occurrence)
            run_binding = created["conversation"]
            occurrence.binding_id = str(run_binding["id"])
            occurrence.message_cursor = created.get("cursor")
            if schedule.conversation_mode == "CONTINUE":
                schedule.binding_id = occurrence.binding_id
        occurrence.state = "STARTED"
        schedule.last_run_at = now()
    except DomainError as exc:
        occurrence.state = "FAILED"
        occurrence.error_detail = f"{exc.code}: {exc.message}"
    db.flush()
