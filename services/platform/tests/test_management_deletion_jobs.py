"""Database-backed durable deletion, progress, retries, and authorization."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_work_directory_deletion import graph as deletion_graph

from flowweave.bootstrap.worker import TaskWorker
from flowweave.modules.agent_sessions.application import management_deletion as jobs
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.tasks.application import service as tasks
from flowweave.modules.users.application.security import USER_USER_ID, tenant_user
from flowweave.runtime.base import RuntimeInputReadiness
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, TaskState

graph = deletion_graph


def claim(g):
    result = tasks.claim(
        g.db, "test-worker", lease_seconds=60, task_types=frozenset({jobs.TASK_TYPE})
    )
    assert result
    return result[1]


def test_submission_accepts_large_selection_without_native_work_and_survives_new_session(
    graph, client, db_session_factory, monkeypatch
):
    g = graph
    bindings = [g.binding() for _ in range(120)]
    ids = [binding.id for binding in bindings]
    finish(g.db)
    monkeypatch.setattr(
        g.runtime, "input_readiness", lambda _: pytest.fail("API must not call Runtime")
    )
    response = client.post(
        f"/api/v1/agent-workspaces/{g.host.id}/conversation-management/deletion-jobs",
        json={"binding_ids": ids},
        headers={"Idempotency-Key": "submit-120"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "PENDING"
    with db_session_factory() as other:
        restored = jobs.listing(other, g.host.id)["items"][0]
        assert restored["id"] == response.json()["id"]
        assert restored["remaining_ids"] == ids
        assert restored["processed"] == 0
        assert all(other.get(AgentConversationBinding, item) for item in ids)
    repeat = client.post(
        f"/api/v1/agent-workspaces/{g.host.id}/conversation-management/deletion-jobs",
        json={"binding_ids": ids},
        headers={"Idempotency-Key": "submit-120"},
    )
    assert repeat.status_code == 202
    assert repeat.json()["id"] == response.json()["id"]


def test_worker_checkpoints_then_retries_only_unfinished_conversations(
    graph, monkeypatch, db_session_factory
):
    g = graph
    bindings = [g.binding() for _ in range(3)]
    ids = [item.id for item in bindings]
    native_ids = [item.openhands_conversation_id for item in bindings]
    uploads = [g.attachment(item)[0] for item in ids]
    started = jobs.start(g.db, g.host.id, tuple(ids), "retry-job")
    finish(g.db)
    lease = claim(g)
    calls = []
    original = g.runtime.delete_conversation
    fail = True

    def delete_native(handle):
        calls.append(handle.conversation_id)
        if handle.conversation_id == native_ids[1] and fail:
            # A fresh connection can read progress while Runtime work is in flight.
            with db_session_factory() as reader:
                current = jobs.listing(reader, g.host.id)["items"][0]
                assert current["deleted_ids"] == ids[:1]
                assert current["processed"] == 1
                assert tasks.heartbeat(reader, lease, lease_seconds=60)
            raise DomainError("RUNTIME_UNAVAILABLE", "private upstream details", 503)
        original(handle)

    monkeypatch.setattr(g.runtime, "delete_conversation", delete_native)
    with pytest.raises(DomainError):
        jobs.process(g.db, g.host.id, lease)
    assert tasks.fail(g.db, lease, "SAFE_ERROR", retry_delay_seconds=1)
    snapshot = jobs.listing(g.db, g.host.id)["items"][0]
    assert snapshot["state"] == "RETRY"
    assert snapshot["deleted_ids"] == ids[:1]
    assert not uploads[0].exists() and uploads[1].exists()
    fail = False
    task = g.db.get(BackgroundTask, started["id"])
    task.available_at = now() - timedelta(seconds=1)
    finish(g.db)
    next_lease = claim(g)
    jobs.process(g.db, g.host.id, next_lease)
    assert tasks.succeed(g.db, next_lease)
    snapshot = jobs.listing(g.db, g.host.id)["items"][0]
    assert snapshot["state"] == "SUCCEEDED"
    assert snapshot["processed"] == 3
    assert snapshot["remaining_ids"] == []
    assert calls.count(native_ids[0]) == 1
    assert not any(path.exists() for path in uploads)


def test_running_and_changed_scope_are_skipped_without_deleting_workspace(graph, monkeypatch):
    g = graph
    directory = g.directory("project")
    running, moved = [g.binding(directory) for _ in range(2)]
    ids = (running.id, moved.id)
    jobs.start(g.db, g.host.id, ids, "skip-job", work_directory_id=directory["id"])
    moved.work_directory_version_id = None
    finish(g.db)
    monkeypatch.setattr(
        g.runtime,
        "input_readiness",
        lambda _: RuntimeInputReadiness(ready=False, execution_status="running"),
    )
    lease = claim(g)
    jobs.process(g.db, g.host.id, lease)
    tasks.succeed(g.db, lease)
    snapshot = jobs.listing(g.db, g.host.id)["items"][0]
    assert snapshot["deleted_ids"] == []
    assert {item["code"] for item in snapshot["skipped"]} == {
        "AGENT_CONVERSATION_RUNNING",
        "AGENT_CONVERSATION_SCOPE_MISMATCH",
    }
    assert snapshot["processed"] == 2
    assert all(g.db.get(AgentConversationBinding, item) is not None for item in ids)
    assert (g.root / "project/result.txt").exists()


def test_user_scope_idempotency_active_task_and_worker_owner_guards(graph):
    g = graph
    first, second = [g.binding() for _ in range(2)]
    job = jobs.start(g.db, g.host.id, (first.id,), "same-key")
    with pytest.raises(DomainError) as conflict:
        jobs.start(g.db, g.host.id, (second.id,), "same-key")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DomainError) as active:
        jobs.start(g.db, g.host.id, (second.id,), "different-key")
    assert active.value.code == "AGENT_DELETION_IN_PROGRESS"
    finish(g.db)
    lease = claim(g)
    with tenant_user(USER_USER_ID):
        assert jobs.listing(g.db, g.host.id) == {"items": []}
        with pytest.raises(DomainError):
            jobs.process(g.db, g.host.id, lease)
        with pytest.raises(DomainError):
            jobs.start(g.db, g.host.id, (first.id,), "foreign")
        task = g.db.get(BackgroundTask, job["id"])
        with TaskWorker._task_tenant_context(task):
            assert jobs.listing(g.db, g.host.id)["items"][0]["id"] == job["id"]


def test_expired_worker_cannot_delete_and_dead_job_retains_retry_remainder(graph, monkeypatch):
    g = graph
    item = g.binding()
    started = jobs.start(g.db, g.host.id, (item.id,), "fence")
    finish(g.db)
    lease = claim(g)
    task = g.db.get(BackgroundTask, started["id"])
    task.lease_until = now() - timedelta(seconds=1)
    finish(g.db)
    monkeypatch.setattr(g.runtime, "delete_conversation", lambda _: pytest.fail("Expired worker"))
    with pytest.raises(DomainError):
        jobs.process(g.db, g.host.id, lease)
    g.db.rollback()
    task = g.db.get(BackgroundTask, started["id"])
    task.state = TaskState.DEAD
    finish(g.db)
    status = jobs.listing(g.db, g.host.id)["items"][0]
    assert status["remaining_ids"] == [item.id]
    assert status["processed"] == 0
    assert jobs.start(g.db, g.host.id, (item.id,), "manual-retry")["id"] != started["id"]
    assert (
        len(
            list(
                g.db.scalars(
                    select(BackgroundTask).where(BackgroundTask.task_type == jobs.TASK_TYPE)
                )
            )
        )
        == 2
    )


def test_registered_worker_handler_settles_checkpoints_under_outer_uow(graph):
    from flowweave.modules.tasks.application.handlers import handle
    from flowweave.shared.application.transactions import mark_uow_owned, run_commit_actions

    g = graph
    ids = [g.binding().id for _ in range(2)]
    started = jobs.start(g.db, g.host.id, tuple(ids), "worker-dispatch")
    finish(g.db)
    lease = claim(g)
    task = g.db.get(BackgroundTask, started["id"])
    mark_uow_owned(g.db)
    handle(g.db, task, lease)
    assert tasks.succeed(g.db, lease, commit=False)
    g.db.commit()
    run_commit_actions(g.db)
    result = jobs.listing(g.db, g.host.id)["items"][0]
    assert result["state"] == "SUCCEEDED"
    assert result["deleted_ids"] == ids
