"""Real PostgreSQL delivery receipts, Worker leases and native identity fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from test_case_event_inbox import emit, event_case  # noqa: F401
from test_case_flow_tools import flow_case  # noqa: F401
from test_case_native_tools import native_case  # noqa: F401

from flowweave.bootstrap.worker import _DELIVERY_TASK_TYPES, TaskWorker
from flowweave.modules.cases.application import feedback, native_tools, service
from flowweave.modules.cases.infrastructure.models import CaseExecutionEvent, CaseFeedbackDelivery
from flowweave.modules.model_providers.infrastructure.models import ProviderModel
from flowweave.modules.tasks.application.handlers import handle, record_terminal_failure
from flowweave.modules.tasks.application.service import (
    TaskDeferred,
    claim,
    defer,
    fail,
    recover_expired,
    succeed,
)
from flowweave.modules.users.application.security import tenant_bypass, tenant_user
from flowweave.runtime.base import RuntimeHandle, RuntimeInputReadiness, RuntimeResult
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState
from flowweave.shared.schemas import CaseSessionConfigWrite, CaseUpdateWrite


@pytest.fixture
def feedback_case(event_case, monkeypatch):  # noqa: F811
    g = event_case
    g.binding.lifecycle = "ACTIVE"
    model = g.db.scalar(
        select(ProviderModel).where(ProviderModel.provider_id == g.preset["model_provider_id"])
    )
    model.is_default = True
    g.db.commit()
    g.posts, g.runs = [], []
    g.status = "finished"
    g.run_status = "RUNNING"
    g.handle = RuntimeHandle("case-test", g.binding.openhands_conversation_id)

    def append(handle, content):
        assert not g.db.in_transaction(), "native I/O must not retain a DB transaction"
        g.posts.append((handle, content))
        return RuntimeResult(status="IDLE", cursor="native-user-" + str(len(g.posts)))

    def run(handle, event_id):
        assert not g.db.in_transaction()
        g.runs.append((handle, event_id))
        return RuntimeResult(status=g.run_status, cursor=event_id)

    runtime = SimpleNamespace(
        input_readiness=lambda _: RuntimeInputReadiness(True, g.status),
        append_message=append,
        run_pending_message=run,
        conversation_mcp_configured=lambda *_: True,
    )
    g.runtime = runtime
    monkeypatch.setattr(feedback, "get_runtime", lambda: runtime)
    monkeypatch.setattr(feedback.conversations, "usage_reconciliation_handle", lambda *_: g.handle)
    monkeypatch.setattr(
        native_tools,
        "coordinator_tool_endpoints",
        lambda *_: (("case", "https://example.test/case"),),
    )
    return g


def deliveries(g):
    return list(
        g.db.scalars(
            select(CaseFeedbackDelivery)
            .order_by(
                CaseFeedbackDelivery.created_at,
                CaseFeedbackDelivery.id,
            )
            .execution_options(populate_existing=True)
        )
    )


def enqueue_event(g, kind="RUNTIME_FAILED"):
    source = emit(g, kind)
    g.db.commit()
    return source


def take(g):
    for task in g.db.scalars(
        select(BackgroundTask).where(
            BackgroundTask.task_type == "DELIVER_CASE_FEEDBACK",
            BackgroundTask.state.in_([TaskState.PENDING, TaskState.RETRY]),
        )
    ):
        task.available_at = now() - timedelta(seconds=1)
    g.db.commit()
    claimed = claim(
        g.db,
        "case-feedback-test",
        lease_seconds=60,
        task_types=frozenset({"DELIVER_CASE_FEEDBACK"}),
    )
    assert claimed is not None
    return claimed


def execute(g, *, expected_wait=None):
    task, lease = take(g)
    if expected_wait:
        with pytest.raises(TaskDeferred, match=expected_wait) as waiting:
            handle(g.db, task, lease)
        assert defer(g.db, lease, waiting.value)
    else:
        handle(g.db, task, lease)
        assert succeed(g.db, lease)
    return task, lease


def test_execution_source_atomic_batch_and_receipt_replay(feedback_case):
    g = feedback_case
    first, second = enqueue_event(g), enqueue_event(g, "ATTEMPT_WAITING_INPUT")
    assert len(deliveries(g)) == 2
    execute(g)
    rows = deliveries(g)
    assert {row.state for row in rows} == {"DELIVERED"}
    assert len({row.batch_id for row in rows}) == len({row.message_event_id for row in rows}) == 1
    assert len(g.posts) == len(g.runs) == 1
    for source in (first, second):
        assert str(source.cursor) in g.posts[0][1]
    assert "read_case_material" in g.posts[0][1]
    execute(g)  # A second member's task consumes the same receipt.
    assert len(g.posts) == len(g.runs) == 1
    assert len(service.get_case(g.db, g.item.id)["feedback_deliveries"]) == 2


def test_running_waits_without_failure_budget_or_new_batch(feedback_case):
    g = feedback_case
    enqueue_event(g)
    g.status = "running"
    for _ in range(4):
        task, _ = execute(g, expected_wait="NOT_IDLE")
        assert task.attempts == 0
    assert not g.posts and deliveries(g)[0].state == "PENDING"
    enqueue_event(g)
    g.status = "finished"
    execute(g)
    execute(g)
    assert len(g.posts) == 2  # The older reserved batch is processed first.


@pytest.mark.parametrize(
    "status", ["paused", "waiting_for_confirmation", "error", "unknown", "stuck"]
)
def test_native_pause_confirmation_and_unknown_are_never_resumed(feedback_case, status):
    g = feedback_case
    enqueue_event(g)
    g.status = status
    execute(g, expected_wait="NOT_IDLE")
    assert g.posts == g.runs == []


def test_case_pause_resume_keeps_original_coordinator_and_sources(feedback_case):
    g = feedback_case
    service.set_case_coordination_state(g.db, g.item.id, g.item.row_version, "PAUSED")
    enqueue_event(g)
    assert g.db.scalar(select(CaseExecutionEvent)).routing_state == "HELD"
    execute(g, expected_wait="CASE_PAUSED")
    service.set_case_coordination_state(g.db, g.item.id, g.item.row_version, "ACTIVE")
    execute(g)
    assert {row.binding_id for row in deliveries(g)} == {g.binding.id}
    execute(g)
    assert len(g.posts) == 2 and "CASE_RESUMED" in g.posts[1][1]


@pytest.mark.parametrize("change", ["round", "binding", "execution", "stage", "terminal", "user"])
def test_stale_or_revoked_authority_becomes_audit_only(feedback_case, monkeypatch, change):
    g = feedback_case
    enqueue_event(g)
    if change == "round":
        g.item.current_control_round_id = str(uuid4())
    elif change == "binding":
        g.item.control_conversation_binding_id = str(uuid4())
    elif change == "execution":
        g.stage.current_execution_flow_run_id = None
    elif change == "stage":
        g.stage.state = "COMPLETED"
    elif change == "terminal":
        g.item.state = "COMPLETED"
    else:
        monkeypatch.setattr(native_tools, "agent_session_principal", lambda *_: None)
    g.db.commit()
    execute(g)
    assert deliveries(g)[0].state == "AUDIT_ONLY" and not g.posts


def test_append_unknown_is_quarantined_and_blocks_later_automatic_delivery(feedback_case):
    g = feedback_case
    enqueue_event(g)

    def uncertain(*args):
        g.posts.append(args)
        raise DomainError(
            "OPENHANDS_REQUEST_FAILED", "network lost", 504, {"outcome_unknown": True}
        )

    g.runtime.append_message = uncertain
    execute(g)
    assert deliveries(g)[0].state == "AMBIGUOUS" and not g.runs
    enqueue_event(g)
    execute(g, expected_wait="NEEDS_RECONCILIATION")
    assert len(g.posts) == 1


def test_crash_after_append_intent_never_reposts(feedback_case):
    g = feedback_case
    enqueue_event(g)
    row = deliveries(g)[0]
    row.batch_id, row.state = row.id, "SUBMITTING"
    g.db.commit()
    execute(g)
    assert deliveries(g)[0].state == "AMBIGUOUS" and not g.posts and not g.runs


def test_run_failure_and_expired_lease_recover_only_native_run(feedback_case):
    g = feedback_case
    enqueue_event(g)
    original = g.runtime.run_pending_message

    def capacity(*args):
        raise DomainError(
            "OPENHANDS_REQUEST_FAILED",
            "capacity",
            429,
            {"status_code": 429, "outcome_unknown": False},
        )

    g.runtime.run_pending_message = capacity
    task, lease = take(g)
    with pytest.raises(DomainError):
        handle(g.db, task, lease)
    g.db.rollback()
    assert deliveries(g)[0].state == "ACKNOWLEDGED"
    assert deliveries(g)[0].message_event_id == "native-user-1"
    task.lease_until = now() - timedelta(seconds=1)
    g.db.commit()
    assert recover_expired(g.db) == 1
    g.runtime.run_pending_message = original
    execute(g)
    assert len(g.posts) == len(g.runs) == 1
    assert deliveries(g)[0].state == "DELIVERED"


def test_pause_between_append_and_run_preserves_ack_without_resuming(feedback_case):
    g = feedback_case
    enqueue_event(g)
    append = g.runtime.append_message

    def append_then_pause(*args):
        result = append(*args)
        g.item.state = "PAUSED"
        g.db.commit()
        return result

    g.runtime.append_message = append_then_pause
    execute(g, expected_wait="CASE_PAUSED")
    assert deliveries(g)[0].state == "ACKNOWLEDGED" and not g.runs
    g.item.state = "ACTIVE"
    g.db.commit()
    execute(g)
    assert len(g.posts) == len(g.runs) == 1


def test_material_and_saved_config_edits_enqueue_once_without_secrets(feedback_case):
    g = feedback_case
    body = {
        key: value
        for key, value in service.get_case(g.db, g.item.id).items()
        if key in {"name", "objective", "description", "work_directory_id", "materials"}
    }
    body["materials"] = [{"kind": "LINK", "name": "new", "value": "https://example.test/new"}]
    service.update_case(
        g.db,
        g.item.id,
        CaseUpdateWrite(
            **body,
            expected_row_version=g.item.row_version,
        ),
    )
    service.save_case_session_config(
        g.db,
        g.item.id,
        CaseSessionConfigWrite(
            **g.preset,
            expected_row_version=g.item.row_version,
        ),
    )
    assert {row.source_kind for row in deliveries(g)} == {
        "MATERIALS_CHANGED",
        "SESSION_CONFIG_CHANGED",
    }
    execute(g)
    assert "test-provider-key" not in g.posts[0][1]
    assert "保存的会话配置变更供后续轮次使用" in g.posts[0][1]
    # A tool configuration result is not separately fed back to itself.
    feedback.record_case_change(g.db, g.item, "SESSION_CONFIG_CHANGED")
    g.db.commit()
    assert len(deliveries(g)) == 2


def test_recovery_only_restores_existing_05a_sources_with_original_owner(feedback_case):
    g = feedback_case
    enqueue_event(g)
    row = deliveries(g)[0]
    g.db.delete(row)
    g.db.commit()
    with tenant_bypass():
        assert feedback.recover_feedback(g.db) == 1
        assert feedback.recover_feedback(g.db) == 0
    g.db.commit()
    saved = deliveries(g)[0]
    assert saved.owner_user_id == g.owner and saved.round_id == g.entry.id
    assert saved.binding_id == g.binding.id


def test_failure_is_durable_and_lease_fences_writes(feedback_case):
    g = feedback_case
    enqueue_event(g)
    task, lease = take(g)
    task.lease_generation += 1
    g.db.commit()
    with pytest.raises(RuntimeError, match="lease was lost"):
        handle(g.db, task, lease)
    assert not g.posts
    task.lease_generation -= 1
    g.db.commit()
    assert fail(g.db, lease, "TEST_FAILURE", permanent=True)
    record_terminal_failure(g.db, task.id, "TEST_FAILURE")
    g.db.commit()
    assert deliveries(g)[0].state == "FAILED"


def test_feedback_queue_forces_rls_and_case_delete_cascade(feedback_case, db_session_factory):
    g = feedback_case
    enqueue_event(g)
    assert g.db.scalar(
        text("SELECT relforcerowsecurity FROM pg_class WHERE relname='case_feedback_deliveries'")
    )
    with tenant_user(str(uuid4())), db_session_factory() as other:
        assert list(other.scalars(select(CaseFeedbackDelivery))) == []
    with tenant_user(g.owner), db_session_factory() as owner:
        owner.execute(text("DELETE FROM cases WHERE id=:id"), {"id": g.item.id})
        owner.commit()
        assert list(owner.scalars(select(CaseFeedbackDelivery))) == []


def test_serialization_lock_prevents_concurrent_native_append(feedback_case, db_session_factory):
    g = feedback_case
    enqueue_event(g)
    task, lease = take(g)
    with feedback._delivery_lock(g.db, g.entry.id):

        def concurrent():
            with tenant_user(g.owner), db_session_factory() as db:
                with pytest.raises(TaskDeferred, match="SERIALIZED"):
                    feedback.deliver_feedback(db, task.aggregate_id, lease)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(concurrent).result(timeout=10)
    assert not g.posts


@pytest.mark.asyncio
async def test_real_worker_deferral_runs_under_owner_and_preserves_retry_budget(
    feedback_case,
    worker_container,
):
    g = feedback_case
    enqueue_event(g)
    g.item.state = "PAUSED"
    g.db.commit()
    task, lease = take(g)
    worker = TaskWorker(worker_container)
    assert "DELIVER_CASE_FEEDBACK" in _DELIVERY_TASK_TYPES
    assert await worker._execute_claimed_task(task, lease)
    g.db.expire_all()
    saved = g.db.get(BackgroundTask, task.id)
    assert saved.state == TaskState.RETRY and saved.attempts == 0
    assert saved.last_error == "CASE_PAUSED"


def test_material_change_during_bootstrap_waits_for_its_original_round(feedback_case):
    g = feedback_case
    g.entry.state = "PENDING"
    g.item.control_conversation_binding_id = None
    g.db.commit()
    feedback.record_case_change(g.db, g.item, "MATERIALS_CHANGED")
    g.db.commit()
    execute(g, expected_wait="COORDINATOR_STARTING")
    assert not g.posts
    g.entry.state = "STARTED"
    g.item.control_conversation_binding_id = g.binding.id
    g.db.commit()
    execute(g)
    assert deliveries(g)[0].state == "DELIVERED" and len(g.posts) == 1


def test_native_pending_run_wait_retains_receipt_and_never_reappends(feedback_case):
    g = feedback_case
    enqueue_event(g)
    g.run_status = "WAITING"
    execute(g, expected_wait="PAUSED_OR_WAITING")
    assert deliveries(g)[0].state == "ACKNOWLEDGED"
    g.run_status = "COMPLETED"
    execute(g)
    assert deliveries(g)[0].state == "DELIVERED" and len(g.posts) == 1


def test_pre_append_failure_retries_without_unknown_submission(feedback_case):
    g = feedback_case
    enqueue_event(g)
    original = g.runtime.append_message

    def unavailable(*_):
        raise DomainError(
            "RUNTIME_MESSAGE_NOT_SUBMITTED", "preflight", 503, {"native_append_started": False}
        )

    g.runtime.append_message = unavailable
    task, lease = take(g)
    with pytest.raises(DomainError):
        handle(g.db, task, lease)
    g.db.rollback()
    assert deliveries(g)[0].state == "PENDING" and not g.posts
    fail(g.db, lease, "preflight")
    g.runtime.append_message = original
    execute(g)
    assert len(g.posts) == len(g.runs) == 1


def test_feedback_creation_failure_rolls_back_formal_source_and_inbox(feedback_case, monkeypatch):
    g = feedback_case
    original = g.db.scalar

    def rejected(statement, *args, **kwargs):
        if getattr(getattr(statement, "table", None), "name", None) == "case_feedback_deliveries":
            raise RuntimeError("feedback insert failed")
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(g.db, "scalar", rejected)
    with pytest.raises(RuntimeError, match="feedback insert failed"):
        emit(g)
    g.db.rollback()
    monkeypatch.setattr(g.db, "scalar", original)
    assert not deliveries(g) and g.db.scalar(select(CaseExecutionEvent)) is None


def test_feedback_migration_reversal_and_database_policy_rejects_foreign_write(feedback_case):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import ProgrammingError

    g = feedback_case
    enqueue_event(g)
    role = "case_feedback_rls_" + uuid4().hex
    with g.db.bind.connect() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, UPDATE ON case_feedback_deliveries TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_feedback_deliveries")) == 0
        connection.execute(
            text("SELECT set_config('flowweave.user_id', :owner, true)"), {"owner": g.owner}
        )
        assert connection.scalar(text("SELECT count(*) FROM case_feedback_deliveries")) == 1
        with pytest.raises(ProgrammingError) as rejected, connection.begin_nested():
            connection.execute(text("UPDATE case_feedback_deliveries SET owner_user_id='other'"))
        assert rejected.value.orig.sqlstate == "42501"
        connection.rollback()
    source = Path(__file__).parents[1] / "migrations/versions/0187_case_feedback.py"
    spec = spec_from_file_location("case_feedback_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    g.db.rollback()
    with g.db.bind.connect() as connection, connection.begin():
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
            assert connection.scalar(text("SELECT count(*) FROM case_feedback_deliveries")) == 0
        connection.rollback()
    g.db.expire_all()
    assert len(deliveries(g)) == 1


def test_pause_resume_api_is_versioned_tenant_scoped_and_rejects_terminal(feedback_case):
    g = feedback_case
    path = f"/api/v1/cases/{g.item.id}/coordination-state"
    current = g.item.row_version
    assert (
        g.client.put(path, json={"state": "PAUSED", "expected_row_version": current}).status_code
        == 200
    )
    assert (
        g.client.put(path, json={"state": "ACTIVE", "expected_row_version": current}).status_code
        == 409
    )
    assert (
        g.client.put(
            path, json={"state": "ACTIVE", "expected_row_version": current + 1}
        ).status_code
        == 200
    )
    g.db.expire_all()
    g.item.state = "COMPLETED"
    g.db.commit()
    assert (
        g.client.put(
            path, json={"state": "ACTIVE", "expected_row_version": current + 2}
        ).status_code
        == 409
    )


def test_newer_task_drains_bounded_backlog_without_losing_its_own_source(feedback_case):
    g = feedback_case
    for _ in range(33):
        emit(g)
    g.db.commit()
    rows = deliveries(g)
    latest = rows[-1]
    for task in g.db.scalars(
        select(BackgroundTask).where(BackgroundTask.task_type == "DELIVER_CASE_FEEDBACK")
    ):
        task.available_at = now() + timedelta(hours=1)
        if task.aggregate_id == latest.id:
            task.available_at = now() - timedelta(seconds=1)
    g.db.commit()
    task, lease = claim(
        g.db, "newest-task-first", lease_seconds=60, task_types=frozenset({"DELIVER_CASE_FEEDBACK"})
    )
    assert task.aggregate_id == latest.id
    with pytest.raises(TaskDeferred, match="BACKLOG") as waiting:
        handle(g.db, task, lease)
    assert defer(g.db, lease, waiting.value)
    assert len([row for row in deliveries(g) if row.state == "DELIVERED"]) == 32
    task.available_at = now() - timedelta(seconds=1)
    g.db.commit()
    task, lease = claim(
        g.db, "newest-task-first", lease_seconds=60, task_types=frozenset({"DELIVER_CASE_FEEDBACK"})
    )
    handle(g.db, task, lease)
    assert succeed(g.db, lease)
    assert len([row for row in deliveries(g) if row.state == "DELIVERED"]) == 33
    assert len(g.posts) == 2


def test_frozen_registration_mismatch_never_appends_or_runs(feedback_case):
    g = feedback_case
    enqueue_event(g)
    g.runtime.conversation_mcp_configured = lambda *_: False
    task, lease = take(g)
    with pytest.raises(DomainError) as failure:
        handle(g.db, task, lease)
    assert failure.value.code == "CASE_TOOL_REGISTRATION_MISMATCH"
    g.db.rollback()
    assert not g.posts and not g.runs and deliveries(g)[0].state == "PENDING"


def test_feedback_registration_preparation_is_local_and_rejects_revoked_grants(
    flow_case,  # noqa: F811
    monkeypatch,
):
    from flowweave.modules.cases.infrastructure.models import CaseMaterialToolGrant

    g = flow_case
    native_tools.prepare_coordinator_tools(g.db, g.entry.id, g.binding)

    def no_network():
        raise AssertionError("registration preparation must not use Runtime I/O")

    monkeypatch.setattr(native_tools, "get_runtime", no_network)
    endpoints = native_tools.coordinator_tool_endpoints(g.db, g.binding.id)
    assert {name for name, _ in endpoints} == {
        native_tools.CONTEXT.server_name,
        native_tools.FLOWS.server_name,
        native_tools.MATERIALS.server_name,
    }
    grant = g.db.scalar(select(CaseMaterialToolGrant))
    grant.token_digest = "0" * 64
    g.db.flush()
    with pytest.raises(DomainError):
        native_tools.coordinator_tool_endpoints(g.db, g.binding.id)
