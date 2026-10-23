from datetime import timedelta

import pytest
from sqlalchemy import select

from flowweave.bootstrap.worker import _DELIVERY_TASK_TYPES
from flowweave.modules.agent_sessions.application import schedules
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.agent_workspaces.application.service import ensure_default_agent_workspace
from flowweave.modules.tasks.infrastructure.models import BackgroundTask
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, tenant_user
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import settings_context


@pytest.fixture(autouse=True)
def schedule_context(settings):
    with settings_context(settings), tenant_user(FLOWWEAVE_USER_ID):
        yield


def test_agent_conversation_schedule_scans_and_enqueues_one_occurrence(
    settings, db_session_factory
):
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="每日检查",
            prompt="检查昨日结果并给出摘要。",
            cron_expression="0 9 * * 1-5",
            workspace_mode="AUTOMATION",
            work_directory_id=None,
            conversation_mode="NEW",
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(),
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
        assert task.aggregate_type == "AGENT_CONVERSATION_SCHEDULE_OCCURRENCE"
        assert task.owner_user_id == schedule.owner_user_id
        assert schedule.next_run_at is not None
        assert schedule.next_run_at > now()


def test_automation_work_directory_is_hidden_from_conversation_workspace_list(
    settings, db_session_factory
):
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="每日检查",
            prompt="检查昨日结果并给出摘要。",
            cron_expression="0 9 * * 1-5",
            workspace_mode="AUTOMATION",
            work_directory_id=None,
            conversation_mode="NEW",
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(),
        )

        assert created["work_directory_id"] is not None
        assert work_directories.list_work_directories(db, workspace.id)["items"] == []


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
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="报告提醒",
            prompt="生成今天的项目报告。",
            cron_expression="0 9 * * *",
            workspace_mode="AUTOMATION",
            work_directory_id=None,
            conversation_mode="NEW",
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(),
        )
        schedule = db.get(AgentConversationSchedule, created["id"])
        assert schedule is not None
        schedules.trigger_schedule(db, workspace.id, created["id"])
        occurrence = db.scalar(
            select(AgentConversationScheduleOccurrence).where(
                AgentConversationScheduleOccurrence.schedule_id == created["id"]
            )
        )
        assert occurrence is not None
        run_binding_id = occurrence.binding_id

        expected_prompt = schedules._automation_prompt(schedule)
        schedules.deliver_occurrence(db, occurrence.id)

        assert created_runs == [
            {
                "workspace_id": workspace.id,
                "work_directory_id": created["work_directory_id"],
                "model_provider_id": "provider-1",
                "model_name": "model-1",
                "reasoning_effort": None,
                "content": expected_prompt,
                "capability_version_ids": (),
                "credential_ids": (),
                "conversation_id": run_binding_id,
                "idempotency_key": f"schedule:{created['id']}:{occurrence.id}",
            }
        ]
        assert occurrence.binding_id != created["work_directory_id"]
        assert occurrence.state == "STARTED"
        assert occurrence.message_cursor == "scheduled-user-event"


@pytest.mark.parametrize("initial_mode", ["NEW", "CONTINUE"])
def test_continue_mode_reuses_latest_run_after_switch_and_repeated_delivery(
    settings, db_session_factory, monkeypatch, initial_mode
):
    from uuid import uuid4

    sent = []
    created_ids = []

    def bootstrap(db, workspace_id, **kwargs):
        binding_id = kwargs["conversation_id"]
        created_ids.append(binding_id)
        db.add(
            AgentConversationBinding(
                id=binding_id,
                workspace_id=workspace_id,
                runtime_session_id=str(uuid4()),
                host_id=workspace_id,
                conversation_scope_id=workspace_id,
                openhands_conversation_id=str(uuid4()),
                create_idempotency_key=kwargs["idempotency_key"],
                lifecycle="ACTIVE",
                display_title="原会话",
                unread=True,
                unread_origin="MANUAL",
            )
        )
        db.flush()
        return {"conversation": {"id": binding_id}, "cursor": "first-event"}

    def message(db, workspace_id, binding_id, content):
        sent.append((workspace_id, binding_id, content))
        return {"cursor": f"continued-{len(sent)}"}

    monkeypatch.setattr(schedules.conversations, "bootstrap_conversation", bootstrap)
    monkeypatch.setattr(schedules.conversations, "message", message)
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="复用检查",
            prompt="继续检查",
            cron_expression="0 9 * * *",
            workspace_mode="AUTOMATION",
            work_directory_id=None,
            conversation_mode=initial_mode,
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(),
        )
        schedule = db.get(AgentConversationSchedule, created["id"])
        first = schedules._enqueue_occurrence(
            db, schedule, trigger_kind="MANUAL", scheduled_for=None
        )
        schedules.deliver_occurrence(db, first.id)
        db.commit()
        original_id = first.binding_id
        updated = schedules.update_schedule(
            db,
            workspace.id,
            schedule.id,
            expected_row_version=schedule.row_version,
            name=schedule.name,
            prompt=schedule.prompt,
            cron_expression=schedule.cron_expression,
            conversation_mode="CONTINUE",
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(),
        )
        assert updated["binding_id"] == original_id
        for kind in ("MANUAL", "SCHEDULED"):
            # Simulate an older template lacking its continuation binding too.
            schedule.binding_id = None
            occurrence = schedules._enqueue_occurrence(
                db, schedule, trigger_kind=kind, scheduled_for=None
            )
            schedules.deliver_occurrence(db, occurrence.id)
            db.commit()
            assert occurrence.binding_id == original_id
            assert occurrence.state == "STARTED"
            schedules.deliver_occurrence(db, occurrence.id)  # delivery retry is a no-op
        assert created_ids == [original_id]
        assert len(sent) == 2
        assert all(item[1] == original_id and item[2].endswith("继续检查") for item in sent)
        rows = schedules.list_occurrences(db, workspace.id, schedule.id)
        assert {row["binding_id"] for row in rows} == {original_id}
        assert rows[0]["conversation"]["unread"] is True
        assert rows[0]["conversation"]["unread_origin"] == "MANUAL"


def test_scheduled_files_download_in_bound_scope_without_exposing_other_automations(
    settings, db_session_factory
):
    from uuid import uuid4

    from flowweave.modules.agent_workspaces.application import workspace as workspace_files

    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        directory = work_directories.create_automation_work_directory(
            db, workspace.id, str(uuid4()), "导出检查"
        )
        version = directory["current_version"]
        root = workspace_files.ensure_user_project_root(db, workspace.id)
        relative_path = version["selected_paths"][0]
        report = root / relative_path / "report.xlsx"
        report.write_bytes(b"xlsx-fixture")
        binding = AgentConversationBinding(
            workspace_id=workspace.id,
            runtime_session_id=str(uuid4()),
            host_id=workspace.id,
            conversation_scope_id=workspace.id,
            openhands_conversation_id=str(uuid4()),
            create_idempotency_key="schedule-download",
            lifecycle="ACTIVE",
            work_directory_version_id=version["id"],
            working_directory=version["working_directory"],
        )
        db.add(binding)
        db.flush()
        runtime_path = f"{version['working_directory']}/report.xlsx"
        downloaded = workspace_files.download(db, workspace.id, runtime_path, binding_id=binding.id)
        assert downloaded.content == b"xlsx-fixture"
        assert downloaded.filename == "report.xlsx"
        assert downloaded.content_type.endswith("spreadsheetml.sheet")
        with pytest.raises(DomainError):
            workspace_files.download(db, workspace.id, runtime_path)
        other = work_directories.create_automation_work_directory(
            db, workspace.id, str(uuid4()), "其他任务"
        )
        with pytest.raises(DomainError):
            workspace_files.download(db, workspace.id, runtime_path, work_directory_id=other["id"])
