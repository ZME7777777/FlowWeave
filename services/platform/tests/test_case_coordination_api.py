import asyncio
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from flowweave.modules.cases.application import coordination, service
from flowweave.modules.cases.infrastructure.models import Case, CaseControlRound, CaseStage
from flowweave.modules.flows.infrastructure.models import FlowDefinition
from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.tasks.application import handlers
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState
from flowweave.shared.schemas import CaseControlConversationWrite


def test_worker_bootstraps_case_round_in_its_owner_context(
    coordination_case, db_session_factory, worker_container, monkeypatch
):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.users.application.security import current_user_id

    owner, case_id, config = coordination_case
    with tenant_user(owner), db_session_factory() as db:
        round_id = service.create_case_control_conversation(db, case_id, config, "worker-delivery")[
            "current_control_round_id"
        ]
    calls = []

    def native(db, workspace_id, **kwargs):
        calls.append(current_user_id())
        assert db.get(Case, case_id).control_conversation_binding_id == kwargs["conversation_id"]
        assert service.get_case(db, case_id)["control_conversation_binding_id"] is None
        db.commit()
        # A user may edit materials while the external native call is pending.
        with tenant_user(owner), db_session_factory() as writer:
            item = writer.get(Case, case_id)
            item.materials_json = [{"kind": "LINK", "name": "new", "value": "new-evidence"}]
            item.row_version += 1
            writer.commit()
        return {"conversation": {"id": kwargs["conversation_id"]}}

    monkeypatch.setattr(coordination.conversations, "bootstrap_conversation", native)
    worker = TaskWorker(worker_container)
    assert asyncio.run(worker.run_once(task_types=frozenset({"BOOTSTRAP_CASE_CONTROL_ROUND"})))
    with tenant_user(owner), db_session_factory() as db:
        assert db.get(CaseControlRound, round_id).state == "STARTED"
        assert db.get(Case, case_id).materials_json[0]["value"] == "new-evidence"
        assert db.get(Case, case_id).row_version == 4
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == round_id))
        assert task.state == TaskState.SUCCEEDED
    assert calls == [owner]


@pytest.fixture
def coordination_case(client, db_session_factory, monkeypatch):
    owner = client.get("/api/v1/auth/me").json()["id"]
    config = CaseControlConversationWrite(model_provider_id="provider", model_name="model")
    workspace_id = str(uuid4())
    with tenant_user(owner), db_session_factory() as db:
        definition = FlowDefinition(name="coordination fixture")
        item = Case(
            name="自主协调",
            objective="交付完整证据",
            state="ACTIVE",
            control_work_directory_id=str(uuid4()),
            session_config_json=config.model_dump(),
        )
        db.add_all([definition, item])
        db.flush()
        template = FlowRun(
            flow_definition_id=definition.id,
            parent_flow_run_id=str(uuid4()),
            run_no=1,
            name="source",
            state="DRAFT",
            run_mode="AUTOMATIC",
            automation_plan_json={"start_node_key": "start", "readiness": {"ready": True}},
        )
        db.add(template)
        db.flush()
        db.add(
            CaseStage(
                case_id=item.id,
                stage_key="one",
                name="阶段",
                position=0,
                state="ACTIVE",
                execution_mode="CONTINUOUS",
                flow_run_id=template.id,
            )
        )
        db.commit()
        case_id = item.id
    monkeypatch.setattr(
        service.agent_workspaces,
        "ensure_default_agent_workspace",
        lambda db: SimpleNamespace(id=workspace_id),
    )
    monkeypatch.setattr(service, "write_case_files", lambda db, item: None)
    monkeypatch.setattr(
        service, "validate_case_session_config", lambda db, value: value.model_dump()
    )
    monkeypatch.setattr(service.conversations, "assert_conversation_stopped", lambda *args: None)
    return owner, case_id, config


def test_http_round_reservation_is_durable_idempotent_and_tenant_scoped(
    client, coordination_case, db_session_factory, monkeypatch
):
    owner, case_id, config = coordination_case
    calls = []
    monkeypatch.setattr(
        coordination.conversations, "bootstrap_conversation", lambda *args, **kw: calls.append(kw)
    )
    url = f"/api/v1/cases/{case_id}/control-conversation"
    first = client.post(url, json=config.model_dump(), headers={"Idempotency-Key": "same-submit"})
    assert first.status_code == 201, first.text
    round_id = first.json()["current_control_round_id"]
    second = client.post(url, json=config.model_dump(), headers={"Idempotency-Key": "same-submit"})
    assert second.json()["current_control_round_id"] == round_id
    assert first.json()["control_conversation_binding_id"] is None and calls == []
    with tenant_user(owner), db_session_factory() as db:
        tasks = list(
            db.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.task_type == "BOOTSTRAP_CASE_CONTROL_ROUND"
                )
            )
        )
        assert len(tasks) == 1 and tasks[0].aggregate_id == round_id
        assert (
            db.get(CaseControlRound, round_id).context_snapshot_json["objective"] == "交付完整证据"
        )
    detail = client.get(f"/api/v1/cases/{case_id}/control-rounds/{round_id}")
    assert detail.status_code == 200
    assert detail.json()["session_config"] == config.model_dump()
    with tenant_user(str(uuid4())), db_session_factory() as db:
        assert db.get(CaseControlRound, round_id) is None
        with pytest.raises(DomainError):
            coordination.get_control_round(db, case_id, round_id)


def test_concurrent_reservations_serialize_on_case_and_enqueue_once(
    coordination_case, db_session_factory
):
    owner, case_id, config = coordination_case

    def reserve():
        with tenant_user(owner), db_session_factory() as db:
            return service.create_case_control_conversation(db, case_id, config, "concurrent")[
                "current_control_round_id"
            ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: reserve(), range(2)))
    assert first == second
    with tenant_user(owner), db_session_factory() as db:
        assert len(list(db.scalars(select(CaseControlRound)))) == 1
        assert (
            len(
                list(db.scalars(select(BackgroundTask).where(BackgroundTask.aggregate_id == first)))
            )
            == 1
        )


def test_delivery_lock_survives_native_commits_and_worker_replay(
    coordination_case, db_session_factory, monkeypatch
):
    owner, case_id, config = coordination_case
    with tenant_user(owner), db_session_factory() as db:
        round_id = service.create_case_control_conversation(db, case_id, config, "delivery")[
            "current_control_round_id"
        ]
    entered, release = Event(), Event()
    calls = []

    def native(db, workspace_id, **kwargs):
        calls.append(kwargs)
        db.commit()
        entered.set()
        assert release.wait(10)
        return {"conversation": {"id": kwargs["conversation_id"]}, "cursor": "formal-message"}

    monkeypatch.setattr(coordination.conversations, "bootstrap_conversation", native)

    def deliver():
        with tenant_user(owner), db_session_factory() as db:
            coordination.deliver_control_round(db, round_id)
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(deliver)
        try:
            assert entered.wait(10)
            with pytest.raises(DomainError) as error:
                deliver()
            assert error.value.code == "CASE_AGENT_DELIVERY_BUSY"
        finally:
            release.set()
        running.result(timeout=10)
    deliver()
    assert len(calls) == 1
    with tenant_user(owner), db_session_factory() as db:
        entry = db.get(CaseControlRound, round_id)
        assert entry.state == "STARTED" and entry.message_cursor == "formal-message"
        assert db.get(Case, case_id).control_conversation_binding_id == entry.binding_id


def test_exhausted_worker_delivery_marks_round_failed_and_http_retry_reuses_it(
    client, coordination_case, db_session_factory
):
    owner, case_id, config = coordination_case
    with tenant_user(owner), db_session_factory() as db:
        round_id = service.create_case_control_conversation(db, case_id, config, "exhausted")[
            "current_control_round_id"
        ]
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == round_id))
        task.state = TaskState.DEAD
        handlers.record_terminal_failure(db, task.id, "protected worker error")
        db.commit()
        assert db.get(CaseControlRound, round_id).error_code == "CASE_AGENT_DELIVERY_EXHAUSTED"
    retried = client.post(f"/api/v1/cases/{case_id}/control-rounds/{round_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["current_control_round_id"] == round_id
    with tenant_user(owner), db_session_factory() as db:
        assert db.get(CaseControlRound, round_id).delivery_attempt == 2
        # A late terminal callback from the previous delivery cannot poison it.
        handlers.record_terminal_failure(db, task.id, "late")
        assert db.get(CaseControlRound, round_id).state == "PENDING"
        assert (
            len(
                list(
                    db.scalars(
                        select(BackgroundTask).where(BackgroundTask.aggregate_id == round_id)
                    )
                )
            )
            == 2
        )


def test_round_migration_backfills_legacy_without_inventing_snapshot_and_enforces_rls(
    db_session_factory,
):
    source = Path(__file__).parents[1] / "migrations/versions/0179_case_control_rounds.py"
    spec = spec_from_file_location("case_round_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = db_session_factory.kw["bind"]
    schema = "case_round_test_" + uuid4().hex
    role = "case_round_reader_" + uuid4().hex
    with engine.connect() as connection, connection.begin():
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(
            text(
                "CREATE TABLE cases (id text PRIMARY KEY, owner_user_id text, "
                "control_conversation_binding_id text, control_work_directory_id text)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_conversation_bindings (id text PRIMARY KEY, "
                "owner_user_id text, "
                "workspace_id text, created_at timestamptz)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO cases VALUES "
                "('legacy-case', 'owner', 'native-binding', 'case-directory')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_conversation_bindings VALUES "
                "('native-binding', 'owner', 'workspace', now())"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        entry = connection.execute(text("SELECT * FROM case_control_rounds")).mappings().one()
        assert entry["binding_id"] == "native-binding"
        assert entry["session_config_json"] == {} and entry["context_snapshot_json"] == {
            "legacy": True
        }
        assert connection.scalar(text("SELECT current_control_round_id FROM cases")) == entry["id"]
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT SELECT ON case_control_rounds TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_control_rounds")) == 0
        connection.execute(text("SELECT set_config('flowweave.user_id', 'owner', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_control_rounds")) == 1
        connection.execute(text("RESET ROLE"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert (
            connection.scalar(text("SELECT control_conversation_binding_id FROM cases"))
            == "native-binding"
        )
        # Roll back schema, test role and all migration effects together.
        connection.rollback()
