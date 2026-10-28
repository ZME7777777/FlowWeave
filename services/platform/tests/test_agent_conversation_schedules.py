from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from flowweave.bootstrap.worker import _DELIVERY_TASK_TYPES
from flowweave.modules.agent_sessions.application import schedules
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.modules.agent_workspaces.application import work_directories
from flowweave.modules.agent_workspaces.application.service import ensure_default_agent_workspace
from flowweave.modules.credentials.application.service import save_credential
from flowweave.modules.tasks.infrastructure.models import BackgroundTask
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, tenant_user
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import WebsiteCredentialWrite
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



@pytest.mark.parametrize("selection", ["unconfigured", "empty", "selected"])
def test_schedule_credential_defaults_preserve_explicit_selections(
    settings, db_session_factory, monkeypatch, selection
):
    created_runs: list[dict[str, object]] = []

    def bootstrap_conversation(db, workspace_id: str, **kwargs):
        del db
        created_runs.append({"workspace_id": workspace_id, **kwargs})
        return {"conversation": {"id": kwargs["conversation_id"]}, "cursor": "scheduled-user-event"}

    monkeypatch.setattr(schedules.conversations, "bootstrap_conversation", bootstrap_conversation)
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        credential_id = save_credential(
            db,
            WebsiteCredentialWrite(
                name="schedule-default",
                target_host="example.test",
                auth_type="TOKEN",
                secret="test-token",
            ),
        )["id"]
        created = schedules.create_schedule(
            db,
            workspace.id,
            name="默认认证",
            prompt="使用默认认证运行。",
            cron_expression="0 9 * * *",
            workspace_mode="AUTOMATION",
            work_directory_id=None,
            conversation_mode="NEW",
            model_provider_id="provider-1",
            model_name="model-1",
            reasoning_effort=None,
            capability_version_ids=(),
            credential_ids=(credential_id,) if selection == "selected" else (),
        )
        schedule = db.get(AgentConversationSchedule, created["id"])
        assert schedule is not None
        schedule.credentials_configured = selection != "unconfigured"
        expected = [] if selection == "empty" else [credential_id]
        assert schedules._schedule_dict(db, schedule)["credential_ids"] == expected

        schedules.trigger_schedule(db, workspace.id, schedule.id)
        occurrence = db.scalar(
            select(AgentConversationScheduleOccurrence).where(
                AgentConversationScheduleOccurrence.schedule_id == schedule.id
            )
        )
        assert occurrence is not None
        schedules.deliver_occurrence(db, occurrence.id)

        assert created_runs[0]["credential_ids"] == tuple(expected)


def test_schedule_credentials_migrate_from_deployed_head(settings, db_session_factory):
    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        credential_id = save_credential(
            db,
            WebsiteCredentialWrite(
                name="migration-credential",
                target_host="example.test",
                auth_type="TOKEN",
                secret="test-token",
            ),
        )["id"]
        expected = {}
        for index, credential_ids in enumerate(((), (credential_id,))):
            created = schedules.create_schedule(
                db, workspace.id, name=f"迁移保留 {index}", prompt="保留认证选择",
                cron_expression="0 9 * * *", workspace_mode="AUTOMATION",
                work_directory_id=None, conversation_mode="NEW",
                model_provider_id="provider-1", model_name="model-1",
                reasoning_effort=None, capability_version_ids=(), credential_ids=credential_ids,
            )
            expected[created["id"]] = list(credential_ids)
        db.commit()

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    try:
        command.downgrade(config, "0177_merge_workspace_credentials")
        command.downgrade(config, "0177_schedule_default_credentials_all-1")
        command.upgrade(config, "0177_merge_workspace_credentials")
        with db_session_factory() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0177_merge_workspace_credentials"
            )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with db_session_factory() as db:
            rows = list(db.scalars(select(AgentConversationSchedule)))
            assert {row.id: row.credential_ids_json for row in rows} == expected
            assert all(row.credentials_configured == bool(expected[row.id]) for row in rows)
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0178_merge_schedule_credentials"
            )
            assert all(db.execute(text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'agent_conversation_schedules'"
            )).one())
    finally:
        command.upgrade(config, "head")


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


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        (
            "report.xlsx",
            b"xlsx-fixture",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "2026-10-23_美股估值增量任务受影响证券摘要.txt",
            "受影响证券文本摘要\n测试内容。\n".encode(),
            "text/plain",
        ),
    ],
)
def test_scheduled_files_download_in_bound_scope_without_exposing_other_automations(
    settings, db_session_factory, filename, content, content_type
):
    from uuid import uuid4

    from flowweave.modules.agent_sessions.application.conversations import project_sandbox_images
    from flowweave.modules.agent_workspaces.application import workspace as workspace_files

    with db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        directory = work_directories.create_automation_work_directory(
            db, workspace.id, str(uuid4()), "导出检查"
        )
        version = directory["current_version"]
        root = workspace_files.ensure_user_project_root(db, workspace.id)
        relative_path = version["selected_paths"][0]
        report = root / relative_path / filename
        report.write_bytes(content)
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
        runtime_path = f"{version['working_directory']}/{filename}"
        projected = project_sandbox_images(
            f"[下载](sandbox:{runtime_path})",
            workspace_id=workspace.id,
            binding_id=binding.id,
            working_directory=version["working_directory"],
        )
        query = parse_qs(urlsplit(projected.removeprefix("[下载](").removesuffix(")")).query)
        assert query == {"path": [runtime_path], "binding_id": [binding.id]}
        for preview in (False, True):
            downloaded = workspace_files.download(
                db,
                workspace.id,
                query["path"][0],
                binding_id=query["binding_id"][0],
                preview=preview,
            )
            assert downloaded.content == content
            assert downloaded.filename == filename
            assert downloaded.content_type == content_type
        with pytest.raises(DomainError):
            workspace_files.download(db, workspace.id, runtime_path)
        other = work_directories.create_automation_work_directory(
            db, workspace.id, str(uuid4()), "其他任务"
        )
        with pytest.raises(DomainError):
            workspace_files.download(db, workspace.id, runtime_path, work_directory_id=other["id"])


def _seed_history(db, count=47):
    from uuid import uuid4

    workspace = ensure_default_agent_workspace(db)
    schedule = AgentConversationSchedule(
        workspace_id=workspace.id, name="分页历史", prompt="检查业务", cron_expression="0 9 * * *"
    )
    db.add(schedule)
    db.flush()
    timestamp = now()
    items = []
    for index in range(count):
        item = AgentConversationScheduleOccurrence(
            schedule_id=schedule.id,
            binding_id=str(uuid4()),
            state="STARTED" if index % 2 else "PENDING",
            trigger_kind="MANUAL" if index % 3 else "SCHEDULED",
            created_at=timestamp - timedelta(seconds=index // 3),
        )
        db.add(item)
        items.append(item)
    db.flush()
    expected = [
        item.binding_id
        for item in sorted(items, key=lambda item: (item.created_at, item.binding_id), reverse=True)
    ]
    db.commit()
    return workspace.id, schedule.id, expected


def test_schedule_conversation_history_pages_all_unique_bindings(client, db_session_factory):
    from sqlalchemy import event

    with db_session_factory() as db:
        workspace, schedule, expected = _seed_history(db)
    url = (
        f"/api/v1/agent-workspaces/{workspace}/conversation-schedules/{schedule}/run-conversations"
    )
    statements = []
    engine = db_session_factory.kw["bind"]

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    from flowweave.modules.agent_sessions.application.schedule_history import list_run_conversations

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with db_session_factory() as db:
            direct = list_run_conversations(db, workspace, schedule, limit=7)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    response = client.get(url, params={"limit": 7})
    assert response.status_code == 200, response.text
    page = response.json()
    assert direct == page
    assert page["total"] == 47 and len(page["items"]) == 7
    assert sum("FROM agent_conversation_bindings" in sql for sql in statements) == 1
    assert not any(
        "runtime_generations" in sql or "agent_conversation_events" in sql for sql in statements
    )
    seen = [item["binding_id"] for item in page["items"]]
    with db_session_factory() as db:
        db.add(
            AgentConversationScheduleOccurrence(
                schedule_id=schedule, binding_id="new-head", state="PENDING", created_at=now()
            )
        )
        db.commit()
    while page["next_cursor"]:
        response = client.get(url, params={"limit": 7, "cursor": page["next_cursor"]})
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["binding_id"] for item in page["items"])
    assert seen == expected and len(seen) == len(set(seen))


def test_schedule_conversation_history_deduplicates_and_keeps_readable_delivery(
    client, db_session_factory
):
    from uuid import uuid4

    with db_session_factory() as db:
        workspace, schedule, bindings = _seed_history(db, 1)
        first = db.scalar(
            select(AgentConversationScheduleOccurrence).where(
                AgentConversationScheduleOccurrence.schedule_id == schedule
            )
        )
        first.state = "STARTED"
        binding = AgentConversationBinding(
            id=bindings[0],
            workspace_id=workspace,
            runtime_session_id=str(uuid4()),
            host_id=workspace,
            conversation_scope_id=str(uuid4()),
            openhands_conversation_id=str(uuid4()),
            display_title="持续会话",
            lifecycle="ACTIVE",
            create_idempotency_key=str(uuid4()),
        )
        db.add(binding)
        timestamp = now()
        for index in range(45):
            db.add(
                AgentConversationScheduleOccurrence(
                    schedule_id=schedule,
                    binding_id=bindings[0],
                    state="PENDING" if index % 2 else "FAILED",
                    error_detail="投递失败",
                    created_at=timestamp + timedelta(seconds=index),
                )
            )
        db.commit()
    url = (
        f"/api/v1/agent-workspaces/{workspace}/conversation-schedules/{schedule}/run-conversations"
    )
    response = client.get(url, params={"limit": 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1 and page["next_cursor"] is None
    assert len(page["items"]) == 1 and page["items"][0]["state"] == "STARTED"
    assert page["items"][0]["conversation_title"] == "持续会话"
    assert page["items"][0]["latest_run_at"] > page["items"][0]["created_at"]


def test_schedule_conversation_history_cursor_scope_and_user_isolation(
    client, user_client, db_session_factory
):
    with db_session_factory() as db:
        workspace, schedule, _ = _seed_history(db, 3)
        _, other, _ = _seed_history(db, 2)
    root = f"/api/v1/agent-workspaces/{workspace}/conversation-schedules"
    url = f"{root}/{schedule}/run-conversations"
    page = client.get(url, params={"limit": 1}).json()
    cursor = page["next_cursor"]
    assert cursor
    assert (
        client.get(f"{root}/{other}/run-conversations", params={"cursor": cursor}).status_code
        == 400
    )
    assert client.get(url, params={"cursor": "invalid"}).status_code == 400
    assert client.get(url, params={"limit": 101}).status_code == 422
    assert (
        client.get(url.replace(workspace, "other-workspace"), params={"cursor": cursor}).status_code
        == 404
    )
    assert user_client.get(url).status_code in (403, 404)
    with db_session_factory() as db:
        empty = AgentConversationSchedule(
            workspace_id=workspace, name="空任务", prompt="检查", cron_expression="0 * * * *"
        )
        db.add(empty)
        db.flush()
        empty_id = empty.id
        db.commit()
    assert client.get(f"{root}/{empty_id}/run-conversations").json() == {
        "items": [],
        "total": 0,
        "next_cursor": None,
    }
