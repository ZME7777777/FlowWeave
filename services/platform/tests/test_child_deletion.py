"""Resumable parent/child deletion using PostgreSQL, native faults and real files."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import test_child_sessions as child_fixtures
from sqlalchemy import select

from flowweave.modules.agent_sessions.application import (
    child_deletion,
    child_launcher,
    flow_node_conversations,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.tasks.application import handlers
from flowweave.modules.tasks.application import service as tasks
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.runtime.base import RuntimeInputReadiness
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask

parent_graph = child_fixtures.parent_graph
parent_profile = child_fixtures.parent_profile
gateway_base_url = child_fixtures.gateway_base_url
launcher_http = child_fixtures.launcher_http
child_http = child_fixtures.child_http
pytestmark = pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)


@pytest.fixture
def deletion(child_http, tmp_path, monkeypatch):
    x = child_http
    root = tmp_path / "shared"
    uploads = root / "uploads"
    uploads.mkdir(parents=True)
    own = uploads / f"{x.loc.binding_id}-{uuid4().hex}--private.txt"
    own.write_text("child attachment")
    parent_file = uploads / f"{x.loc.parent_id}-{uuid4().hex}--private.txt"
    parent_file.write_text("parent attachment")
    other_file = uploads / f"{uuid4()}-{uuid4().hex}--other.txt"
    other_file.write_text("other attachment")
    (root / "project.txt").write_text("shared project")
    x.client.app.state.container.lifecycle_capacity = 0
    x.client.app.state.container.blocking_mutation_slots = asyncio.Semaphore(1)
    x.client.app.state.container.conversation_hydration_cache.invalidate_binding = AsyncMock()
    monkeypatch.setattr(
        flow_node_conversations.agent_sessions,
        "resolve_flow_node_session_host",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        flow_node_conversations, "_node_handle", lambda *_args, **_kwargs: x.loc.parent_handle
    )
    monkeypatch.setattr(
        child_deletion.workspace_host, "agent_workspace_record_path", lambda *_: root
    )
    monkeypatch.setattr(
        child_deletion.sandboxes,
        "node_attempt_workspace_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            host_working_directory=root, runtime_working_directory=x.loc.working_directory
        ),
    )
    state = SimpleNamespace(
        present={x.loc.parent_handle.conversation_id, x.loc.handle.conversation_id},
        calls=[],
        fault=None,
        counter=0,
        after=False,
        resurrect=False,
        require_no_sql=True,
    )

    def remove(handle):
        if state.require_no_sql:
            assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        state.counter += 1
        state.calls.append(handle.conversation_id)
        fail = state.counter == state.fault
        if fail and not state.after:
            raise DomainError("FIXTURE_NATIVE_DELETE_TIMEOUT", "delete outcome unknown", 504)
        if handle.conversation_id not in state.present:
            raise DomainError("RUNTIME_CONVERSATION_MISSING", "native absent", 404)
        state.present.remove(handle.conversation_id)
        if handle.conversation_id == x.loc.parent_handle.conversation_id and state.resurrect:
            # A preparation already admitted before the platform fence finishes
            # before the native parent lifecycle lock releases for deletion.
            state.present.add(x.loc.handle.conversation_id)
        if fail:
            raise DomainError("FIXTURE_NATIVE_DELETE_TIMEOUT", "delete outcome unknown", 504)

    x.runtime.delete_conversation = remove
    x.runtime.conversation_activity_snapshot = lambda _: pytest.fail(
        "pending family is metadata only"
    )
    if x.g.kind == "AGENT_WORKSPACE":
        path = f"/api/v1/agent-workspaces/{x.loc.host_id}/conversations/{x.loc.parent_id}"
        activity_path = f"/api/v1/agent-workspaces/{x.loc.host_id}/conversation-activity"
    else:
        base = f"/api/v1/flow-runs/{x.loc.host_id}/node-attempts/{x.loc.scope_id}/agent-sessions"
        path, activity_path = f"{base}/{x.loc.parent_id}", f"{base}/activity"
    yield SimpleNamespace(
        x=x,
        state=state,
        root=root,
        own=own,
        parent_file=parent_file,
        other_file=other_file,
        path=path,
        activity_path=activity_path,
    )


def request(a):
    a.x.g.db.commit()
    response = a.x.client.delete(a.path)
    assert response.status_code == 202, response.text
    return response.json()


def claim(a):
    a.x.g.db.commit()
    with a.x.g.sessions() as db:
        task, lease = tasks.claim(
            db, "family-test", lease_seconds=120, task_types=frozenset({child_deletion.TASK_TYPE})
        )
    return task, lease


def execute(a, task, lease):
    with a.x.g.sessions() as db, runtime_context(a.x.runtime):
        handlers.handle(db, task, lease)
        assert tasks.succeed(db, lease)


def test_delete_fences_creation_and_writes_but_keeps_parent_and_child_entry(deletion):
    a = deletion
    first = request(a)
    assert request(a)["task_id"] == first["task_id"]
    a.x.g.db.expire_all()
    parent = a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id)
    assert parent.lifecycle == "DELETE_PENDING"
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id).lifecycle == "DELETE_PENDING"
    with pytest.raises(DomainError) as denied:
        child_launcher.prepare_launcher(
            a.x.g.db,
            owner_id=a.x.g.frozen.owner_user_id,
            binding_id=a.x.g.frozen.binding_id,
            runtime_key=a.x.g.key,
        )
    assert denied.value.status == 403
    a.x.g.db.commit()
    assert (
        a.x.client.post(
            a.x.root + "/messages", json={"submission_id": str(uuid4()), "content": "late message"}
        ).status_code
        == 404
    )
    listing = a.x.client.get(a.x.root.rsplit("/", 1)[0])
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == a.x.loc.binding_id
    activity = a.x.client.get(a.activity_path)
    assert activity.status_code == 200, activity.text
    row = activity.json()["conversations"][0]
    assert row["id"] == a.x.loc.parent_id and row["write_available"] is False
    assert row["deletion"]["task_id"] == first["task_id"]
    assert activity.json()["deleting_binding_ids"] == [a.x.loc.parent_id]
    assert a.state.calls == []


def test_worker_deletes_children_before_parent_then_rechecks_and_preserves_shared_files(deletion):
    a = deletion
    info = request(a)
    task, lease = claim(a)
    execute(a, task, lease)
    assert a.state.calls == [
        a.x.loc.handle.conversation_id,
        a.x.loc.parent_handle.conversation_id,
        a.x.loc.handle.conversation_id,
    ]
    assert a.state.present == set()
    a.x.g.db.expire_all()
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id) is None
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id) is None
    assert a.x.g.db.scalar(select(AgentChildConversation)) is None
    audit = a.x.g.db.get(BackgroundTask, info["task_id"])
    assert audit.state == "SUCCEEDED" and audit.payload_json["phase"] == "COMPLETE"
    assert not a.own.exists() and not a.parent_file.exists()
    assert a.other_file.read_text() == "other attachment"
    assert (a.root / "project.txt").read_text() == "shared project"


@pytest.mark.parametrize("fault,after", [(1, False), (1, True), (2, False), (2, True), (3, False)])
def test_partial_native_failure_retains_scope_and_explicit_retry_resumes_same_job(
    deletion, fault, after
):
    a = deletion
    info = request(a)
    task, lease = claim(a)
    a.state.fault, a.state.after = fault, after
    with pytest.raises(DomainError) as unknown:
        execute(a, task, lease)
    assert unknown.value.code == "FIXTURE_NATIVE_DELETE_TIMEOUT"
    with a.x.g.sessions() as db:
        assert tasks.fail(db, lease, "FIXTURE_NATIVE_DELETE_TIMEOUT: protected", permanent=True)
    a.x.g.db.expire_all()
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id).lifecycle == "DELETE_PENDING"
    assert a.x.g.db.scalar(select(AgentChildConversation.id)) is not None
    # Closing browser views has no task cancellation effect. An explicit retry
    # uses the same audit operation and original target binding IDs.
    retried = request(a)
    assert retried["task_id"] == info["task_id"]
    a.state.fault = None
    task, lease = claim(a)
    execute(a, task, lease)
    assert a.state.present == set()
    assert a.other_file.exists() and (a.root / "project.txt").exists()


def test_parent_barrier_collects_preparation_missing_during_first_child_pass(deletion):
    a = deletion
    a.state.present.remove(a.x.loc.handle.conversation_id)
    a.state.resurrect = True
    request(a)
    task, lease = claim(a)
    execute(a, task, lease)
    assert a.state.present == set()
    assert a.state.calls.count(a.x.loc.handle.conversation_id) == 2


def test_expired_worker_cannot_drop_locators_after_native_effect(deletion, monkeypatch):
    a = deletion
    request(a)
    task, lease = claim(a)
    original = a.x.runtime.delete_conversation

    def expire(handle):
        original(handle)
        with a.x.g.sessions() as db:
            job = db.get(BackgroundTask, task.id)
            job.lease_until = now() - timedelta(seconds=1)
            db.commit()

    monkeypatch.setattr(a.x.runtime, "delete_conversation", expire)
    with pytest.raises(DomainError) as lost:
        execute(a, task, lease)
    assert lost.value.code == "AGENT_CHILD_DELETION_LEASE_LOST"
    a.x.g.db.expire_all()
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id) is not None
    assert a.x.g.db.get(BackgroundTask, task.id).payload_json["first_index"] == 0


def test_failed_cleanup_audit_is_retained_while_parent_is_fenced(deletion):
    a = deletion
    info = request(a)
    with a.x.g.sessions() as db:
        task = db.get(BackgroundTask, info["task_id"])
        task.state = "DEAD"
        task.updated_at = now() - timedelta(days=90)
        db.commit()
        tasks.cleanup_terminal(db, retention_days=1, batch_size=100)
        assert db.get(BackgroundTask, info["task_id"]) is not None


@pytest.mark.parametrize("target", ["child", "parent"])
def test_attachment_failure_keeps_checkpoint_and_retry_does_not_lose_locators(
    deletion, monkeypatch, target
):
    a = deletion
    info = request(a)
    task, lease = claim(a)
    original = child_deletion._remove_files
    failing_id = a.x.loc.binding_id if target == "child" else a.x.loc.parent_id

    def fail(db, parent, binding_id, root):
        if binding_id == failing_id:
            raise DomainError("AGENT_ATTACHMENT_CLEANUP_FAILED", "retry cleanup", 503)
        return original(db, parent, binding_id, root)

    monkeypatch.setattr(child_deletion, "_remove_files", fail)
    with pytest.raises(DomainError) as failure:
        execute(a, task, lease)
    assert failure.value.code == "AGENT_ATTACHMENT_CLEANUP_FAILED"
    a.x.g.db.expire_all()
    job = a.x.g.db.get(BackgroundTask, info["task_id"])
    assert job.payload_json["final_index"] == (0 if target == "child" else 1)
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id) is not None
    assert (a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id) is not None) == (
        target == "child"
    )
    a.x.g.db.commit()
    with a.x.g.sessions() as db:
        assert tasks.fail(db, lease, "AGENT_ATTACHMENT_CLEANUP_FAILED", permanent=True)
    monkeypatch.setattr(child_deletion, "_remove_files", original)
    assert request(a)["task_id"] == info["task_id"]
    task, lease = claim(a)
    execute(a, task, lease)
    assert a.state.present == set()
    assert a.other_file.exists()


def test_delayed_sweep_removes_late_upload_without_touching_shared_files(deletion):
    a = deletion
    request(a)
    task, lease = claim(a)
    execute(a, task, lease)
    late = a.root / "uploads" / f"{a.x.loc.binding_id}-{uuid4().hex}--late.txt"
    late.write_text("upload finished after native deletion")
    with a.x.g.sessions() as db:
        cleanup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_DELETED_CONVERSATION_ATTACHMENTS",
                BackgroundTask.payload_json["binding_id"].as_string() == a.x.loc.binding_id,
            )
        )
        assert cleanup is not None and cleanup.available_at > now()
        cleanup.available_at = now()
        db.commit()
        work, token = tasks.claim(
            db,
            "late-upload",
            lease_seconds=60,
            task_types=frozenset({"CLEANUP_DELETED_CONVERSATION_ATTACHMENTS"}),
        )
        handlers.handle(db, work, token)
        assert tasks.succeed(db, token)
    assert not late.exists()
    assert a.other_file.exists() and (a.root / "project.txt").exists()


def test_sweep_never_removes_attachments_of_an_existing_binding(deletion):
    a = deletion
    child_deletion.sweep_deleted_attachments(
        a.x.g.db,
        {
            "binding_id": a.x.loc.binding_id,
            "host_root": str(a.root),
            "runtime_root": a.x.loc.handle.workspace_root,
        },
    )
    assert a.own.exists()


def test_normal_worker_lane_dispatch_restores_task_owner(deletion):
    from flowweave.bootstrap.worker import TaskWorker

    a = deletion
    request(a)
    task, lease = claim(a)
    worker = TaskWorker(a.x.client.app.state.container)
    for concurrency in (1, 2, 3, 4, 6):
        worker.container.settings = a.x.g.config.model_copy(
            update={"worker_concurrency": concurrency}
        )
        memberships = [types for _, types, _ in worker._lane_specs()]
        for name in (child_deletion.TASK_TYPE, "CLEANUP_DELETED_CONVERSATION_ATTACHMENTS"):
            assert sum(name in types for types in memberships) == 1
    # Exercise the real executor/UoW/succeed path outside the graph owner's
    # ambient context. It must restore this task's explicit owner itself.
    with tenant_user(a.x.g.other), runtime_context(a.x.runtime):
        assert asyncio.run(worker._execute_claimed_task(task, lease)) is True
    assert a.state.present == set()


def test_inactive_borrowed_runtime_cannot_discard_child_locators(deletion, monkeypatch):
    a = deletion
    if a.x.g.kind != "FLOW_NODE":
        pytest.skip("FlowRun record cleanup host")

    def inactive(*_args):
        raise DomainError("RUNTIME_SESSION_NOT_ACTIVE", "inactive", 409)

    monkeypatch.setattr(child_deletion, "_route", inactive)
    monkeypatch.setattr(
        child_deletion.sandboxes, "runtime_owner_flow_run_id", lambda *_: "other-run"
    )
    with pytest.raises(DomainError) as rejected:
        child_deletion.cleanup_record_family(a.x.g.db, a.x.g.binding, allow_inactive_runtime=True)
    assert rejected.value.code == "RUNTIME_SESSION_NOT_ACTIVE"
    a.x.g.db.rollback()
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id) is not None
    assert a.state.calls == []


def test_record_worker_restores_owner_before_child_family_cleanup(deletion, monkeypatch):
    from flowweave.bootstrap.worker import TaskWorker

    a = deletion
    if a.x.g.kind != "FLOW_NODE":
        pytest.skip("FlowRun record cleanup host")
    a.state.require_no_sql = False
    parent_id = a.x.loc.parent_id

    def record_cleanup(db, aggregate_id, flow_run_id, *, commit):
        assert current_user_id() == a.x.g.owner and commit is False
        flow_node_conversations.delete_flow_node_conversation_for_record_cleanup(
            db, flow_run_id=flow_run_id, binding_id=parent_id
        )

    monkeypatch.setattr(handlers.orchestration, "process_delete_node_run_record", record_cleanup)
    job = tasks.enqueue(
        a.x.g.db,
        task_type="DELETE_NODE_RUN_RECORD",
        aggregate_type="NODE_RUN",
        aggregate_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        payload={"flow_run_id": a.x.loc.host_id},
    )
    a.x.g.db.commit()
    with a.x.g.sessions() as db:
        task, lease = tasks.claim(
            db, "record-worker", lease_seconds=60, task_types=frozenset({"DELETE_NODE_RUN_RECORD"})
        )
    assert task.id == job.id
    worker = TaskWorker(a.x.client.app.state.container)
    with tenant_user(a.x.g.other), runtime_context(a.x.runtime):
        assert asyncio.run(worker._execute_claimed_task(task, lease)) is True
    assert a.state.present == set()


def test_running_parent_cannot_start_destructive_cleanup(deletion):
    a = deletion
    a.x.runtime.input_readiness = lambda _: RuntimeInputReadiness(False, "running")
    response = a.x.client.delete(a.path)
    assert response.status_code == 409, response.text
    a.x.g.db.expire_all()
    assert a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id).lifecycle == "ACTIVE"
    assert child_deletion.projection(a.x.g.db, a.x.loc.parent_id) is None
    assert a.state.calls == []


def test_record_cleanup_reuses_native_barrier_and_never_commits_outer_uow(deletion):
    a = deletion
    if a.x.g.kind != "FLOW_NODE":
        pytest.skip("FlowRun record cleanup host")
    a.state.require_no_sql = False
    db = a.x.g.db
    parent = db.get(AgentConversationBinding, a.x.loc.parent_id)
    parent.display_title = "uncommitted title"
    with runtime_context(a.x.runtime):
        flow_node_conversations.delete_flow_node_conversation_for_record_cleanup(
            db, flow_run_id=a.x.loc.host_id, binding_id=parent.id
        )
    # A record-level rollback keeps all locators for idempotent native retry.
    db.rollback()
    assert db.get(AgentConversationBinding, a.x.loc.parent_id) is not None
    assert db.get(AgentConversationBinding, a.x.loc.binding_id) is not None
    assert db.get(AgentConversationBinding, a.x.loc.parent_id).display_title != "uncommitted title"
    with runtime_context(a.x.runtime):
        flow_node_conversations.delete_flow_node_conversation_for_record_cleanup(
            db, flow_run_id=a.x.loc.host_id, binding_id=a.x.loc.parent_id
        )
    db.commit()
    assert db.get(AgentConversationBinding, a.x.loc.parent_id) is None
    assert a.state.present == set()


def test_other_user_cannot_request_or_view_family_deletion(deletion):
    a = deletion
    a.x.client.cookies.set("flowweave_session", "other-cookie")
    with tenant_user(a.x.g.other):
        response = a.x.client.delete(a.path)
    assert response.status_code == 404, response.text
    assert a.state.calls == []
    assert not list(
        a.x.g.db.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == child_deletion.TASK_TYPE)
        )
    )


@pytest.mark.parametrize("parent_graph", ["AGENT_WORKSPACE"], indirect=True)
def test_managed_batch_waits_for_family_cleanup_before_counting_deleted(deletion):
    from flowweave.modules.agent_sessions.application import management_deletion as jobs

    a = deletion
    g = a.x.g
    parent_id = a.x.loc.parent_id
    # The child is never an independently selectable bulk-delete target.
    with pytest.raises(DomainError):
        jobs.start(g.db, g.binding.workspace_id, (a.x.loc.binding_id,), "reject-child")
    g.db.rollback()
    job = jobs.start(g.db, g.binding.workspace_id, (parent_id,), "family-batch")
    g.db.commit()
    _, lease = tasks.claim(
        g.db, "batch-family", lease_seconds=120, task_types=frozenset({jobs.TASK_TYPE})
    )
    with runtime_context(a.x.runtime), pytest.raises(DomainError) as pending:
        jobs.process(g.db, g.binding.workspace_id, lease)
    assert pending.value.code == "AGENT_DELETION_FAMILY_PENDING"
    snapshot = jobs.listing(g.db, g.binding.workspace_id)["items"][0]
    assert snapshot["processed"] == 0 and snapshot["deleted_ids"] == []
    assert g.db.get(BackgroundTask, job["id"]).payload_json["pending_ids"] == [parent_id]
    g.db.commit()
    task, family_lease = claim(a)
    execute(a, task, family_lease)
    with runtime_context(a.x.runtime):
        jobs.process(g.db, g.binding.workspace_id, lease)
    assert jobs.listing(g.db, g.binding.workspace_id)["items"][0]["deleted_ids"] == [parent_id]
    assert a.other_file.exists() and (a.root / "project.txt").exists()
