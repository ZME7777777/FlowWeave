from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from flowweave.modules.agent_sessions.application import schedules
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.modules.agent_workspaces.application.service import ensure_default_agent_workspace
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspaceRuntime
from flowweave.modules.tasks.infrastructure.models import BackgroundTask
from flowweave.shared.database import now


def _active_binding(db, workspace_id: str) -> AgentConversationBinding:
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace_id)
    )
    assert runtime is not None
    binding = AgentConversationBinding(
        workspace_id=workspace_id,
        runtime_session_id=runtime.id,
        host_kind="AGENT_WORKSPACE",
        host_id=workspace_id,
        conversation_scope_id=workspace_id,
        working_directory="/runtime/workspace/project",
        openhands_conversation_id=str(uuid4()),
        display_title="每日检查会话",
        lifecycle="ACTIVE",
        create_idempotency_key=f"schedule-test-{uuid4()}",
    )
    db.add(binding)
    db.flush()
    return binding


def test_agent_conversation_schedule_scans_and_enqueues_one_occurrence(
    settings, db_session_factory
):
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        binding = _active_binding(db, workspace.id)
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="每日检查",
            binding_id=binding.id,
            prompt="检查昨日结果并给出摘要。",
            cron_expression="0 9 * * 1-5",
        )
        schedule = db.get(AgentConversationSchedule, created["id"])
        assert schedule is not None
        schedule.next_run_at = now() - timedelta(minutes=1)
        db.commit()

        assert schedules.scan_due_schedules(db) == 1
        occurrence = db.scalar(
            select(AgentConversationScheduleOccurrence).where(
                AgentConversationScheduleOccurrence.schedule_id == schedule.id
            )
        )
        assert occurrence is not None
        assert occurrence.trigger_kind == "SCHEDULED"
        assert occurrence.state == "PENDING"
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == occurrence.id,
                BackgroundTask.task_type == "DELIVER_AGENT_CONVERSATION_SCHEDULE",
            )
        )
        assert task is not None
        assert task.owner_user_id == schedule.owner_user_id
        assert schedule.next_run_at is not None
        assert schedule.next_run_at > now()


def test_agent_conversation_schedule_delivery_uses_native_message_service(
    settings, db_session_factory, monkeypatch
):
    delivered: list[tuple[str, str, str]] = []

    def send_message(db, workspace_id: str, binding_id: str, content: str):
        del db
        delivered.append((workspace_id, binding_id, content))
        return {"accepted": True, "cursor": "scheduled-user-event"}

    monkeypatch.setattr(schedules.conversations, "message", send_message)
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        binding = _active_binding(db, workspace.id)
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="报告提醒",
            binding_id=binding.id,
            prompt="生成今天的项目报告。",
            cron_expression="0 9 * * *",
        )
        schedules.trigger_schedule(db, workspace.id, created["id"])
        occurrence = db.scalar(
            select(AgentConversationScheduleOccurrence).where(
                AgentConversationScheduleOccurrence.schedule_id == created["id"]
            )
        )
        assert occurrence is not None

        schedules.deliver_occurrence(db, occurrence.id)

        assert delivered == [(workspace.id, binding.id, "生成今天的项目报告。")]
        assert occurrence.state == "STARTED"
        assert occurrence.message_cursor == "scheduled-user-event"
