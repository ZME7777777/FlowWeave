from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from flowweave.bootstrap.worker import _DELIVERY_TASK_TYPES
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


def test_agent_conversation_schedule_delivery_is_claimed_by_worker() -> None:
    assert "DELIVER_AGENT_CONVERSATION_SCHEDULE" in _DELIVERY_TASK_TYPES


def test_agent_conversation_schedule_delivery_creates_a_distinct_native_conversation(
    settings, db_session_factory, monkeypatch
):
    created_runs: list[dict[str, object]] = []

    def bootstrap_conversation(db, workspace_id: str, **kwargs):
        del db
        created_runs.append({"workspace_id": workspace_id, **kwargs})
        return {"conversation": {"id": kwargs["conversation_id"]}, "cursor": "scheduled-user-event"}

    monkeypatch.setattr(schedules.conversations, "bootstrap_conversation", bootstrap_conversation)
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        binding = _active_binding(db, workspace.id)
        binding.model_provider_id = "provider-1"
        binding.model_name = "model-1"
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
        run_binding_id = occurrence.binding_id

        schedules.deliver_occurrence(db, occurrence.id)

        assert created_runs == [
            {
                "workspace_id": workspace.id,
                "work_directory_id": None,
                "model_provider_id": "provider-1",
                "model_name": "model-1",
                "reasoning_effort": None,
                "fallback_models": (),
                "content": "生成今天的项目报告。",
                "capability_version_ids": (),
                "conversation_id": run_binding_id,
                "idempotency_key": f"schedule:{created['id']}:{occurrence.id}",
            }
        ]
        assert occurrence.binding_id != binding.id
        assert occurrence.state == "STARTED"
        assert occurrence.message_cursor == "scheduled-user-event"
