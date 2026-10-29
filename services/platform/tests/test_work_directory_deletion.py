"""Exercise workspace deletion with real PostgreSQL and real isolated files."""

from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_agent_workspaces import _ready_workspace_for_conversation

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentAttachmentUpload,
    AgentAttachmentUploadPart,
    AgentConversationBinding,
    AgentConversationMessageAttachment,
    AgentSidebarConversation,
)
from flowweave.modules.agent_workspaces.application import (
    directory_deletion,
    work_directories,
    workspace,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
    AgentWorkDirectoryVersion,
    AgentWorkspaceRuntime,
)
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    tenant_user,
    user_runtime_project_root,
)
from flowweave.runtime.base import RuntimeInputReadiness
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.application.transactions import finish, run_rollback_actions
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask
from flowweave.shared.settings import settings_context


@pytest.fixture
def graph(settings, db_session_factory):
    runtime = MockRuntime()
    with (
        settings_context(settings),
        tenant_user(FLOWWEAVE_USER_ID),
        runtime_context(runtime),
        db_session_factory() as db,
    ):
        host = _ready_workspace_for_conversation(db)
        root = workspace.ensure_user_project_root(db, host.id)
        runtime_id = db.scalar(
            select(AgentWorkspaceRuntime.id).where(AgentWorkspaceRuntime.workspace_id == host.id)
        )

        def directory(name, paths=None):
            for path in paths or [name]:
                (root / path).mkdir(parents=True, exist_ok=True)
                (root / path / "result.txt").write_text(name)
            return work_directories.create_work_directory(db, host.id, name, tuple(paths or [name]))

        def binding(directory=None, archived=True):
            item = AgentConversationBinding(
                workspace_id=host.id,
                host_kind="AGENT_WORKSPACE",
                host_id=host.id,
                runtime_session_id=runtime_id,
                openhands_conversation_id=str(uuid4()),
                work_directory_version_id=directory["current_version"]["id"] if directory else None,
                working_directory=directory["current_version"]["working_directory"]
                if directory
                else user_runtime_project_root(host.id),
                create_idempotency_key=str(uuid4()),
                lifecycle="ACTIVE",
                archived_at=datetime.now(UTC) if archived else None,
            )
            db.add(item)
            db.flush()
            return item

        def attachment(owner):
            uploads = root / "uploads"
            uploads.mkdir(exist_ok=True)
            path = uploads / f"{owner}-{uuid4().hex}--note.txt"
            path.write_text("private")
            return path, str(
                PurePosixPath(user_runtime_project_root(host.id)) / "uploads" / path.name
            )

        yield SimpleNamespace(
            db=db,
            host=host,
            root=root,
            runtime=runtime,
            directory=directory,
            binding=binding,
            attachment=attachment,
        )
        db.rollback()
        run_rollback_actions(db)


def test_delete_archived_directory_removes_all_versions_sessions_uploads_and_sidebar(graph):
    g = graph
    directory = g.directory("project", ["old-a", "old-b"])
    first = g.binding(directory)
    (g.root / "current").mkdir()
    (g.root / "current/new.txt").write_text("new")
    updated = work_directories.update_work_directory(
        g.db, g.host.id, directory["id"], display_name=None, selected_paths=("current",)
    )
    second = g.binding(updated)
    sidebar = g.binding()
    g.db.add(
        AgentSidebarConversation(
            workspace_id=g.host.id,
            source_binding_id=first.id,
            sidebar_binding_id=sidebar.id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    unrelated = g.binding()
    owned = []
    for item in [first, second, sidebar]:
        path, runtime_path = g.attachment(item.id)
        owned.append(path)
        g.db.add(
            AgentConversationMessageAttachment(
                binding_id=item.id,
                event_id="event",
                path=runtime_path,
                filename="note.txt",
                mime_type="text/plain",
                byte_size=7,
            )
        )
    pending, _ = g.attachment(first.id)
    owned.append(pending)
    untouched, _ = g.attachment(unrelated.id)
    draft_owner = str(uuid4())
    draft, _ = g.attachment(draft_owner)
    owned.append(draft)
    upload = AgentAttachmentUpload(
        host_kind="AGENT_WORKSPACE",
        host_id=g.host.id,
        work_directory_id=directory["id"],
        attachment_owner_id=draft_owner,
        filename="note.txt",
        mime_type="text/plain",
        total_size=7,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    g.db.add(upload)
    g.db.flush()
    g.db.add(AgentAttachmentUploadPart(upload_id=upload.id, part_number=0, content=b"private"))
    g.db.get(AgentWorkDirectory, directory["id"]).archived_at = datetime.now(UTC)
    finish(g.db)
    work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    finish(g.db)
    assert all(not (g.root / path).exists() for path in ["old-a", "old-b", "current"])
    assert all(not path.exists() for path in owned)
    assert untouched.read_text() == "private"
    assert g.db.get(AgentConversationBinding, unrelated.id) is not None
    assert g.db.get(AgentWorkDirectory, directory["id"]) is None
    assert g.db.get(AgentAttachmentUpload, upload.id) is None
    assert not list(g.db.scalars(select(AgentSidebarConversation)))
    assert not list((g.root / ".flowweave/deleted-workspaces").iterdir())
    task = g.db.scalar(
        select(BackgroundTask).where(BackgroundTask.task_type == "CLEANUP_AGENT_WORK_DIRECTORY")
    )
    directory_deletion.cleanup(g.db, task.aggregate_id, task.payload_json)  # Idempotent retry.


def test_shared_directory_refuses_deletion_before_native_or_file_changes(graph, monkeypatch):
    g = graph
    directory = g.directory("project", ["shared"])
    g.directory("another", ["shared"])
    item = g.binding(directory)
    finish(g.db)
    monkeypatch.setattr(
        g.runtime,
        "delete_conversation",
        lambda _: pytest.fail("Shared files must be rejected before native deletion"),
    )
    with pytest.raises(DomainError) as caught:
        work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    assert caught.value.code == "AGENT_WORK_DIRECTORY_SHARED_PATH"
    assert (g.root / "shared/result.txt").exists()
    assert g.db.get(AgentConversationBinding, item.id) is not None


def test_running_session_keeps_every_file_and_binding(graph, monkeypatch):
    g = graph
    directory = g.directory("project")
    item = g.binding(directory)
    finish(g.db)
    monkeypatch.setattr(
        g.runtime,
        "input_readiness",
        lambda _: RuntimeInputReadiness(ready=False, execution_status="running"),
    )
    with pytest.raises(DomainError) as caught:
        work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    assert caught.value.code == "AGENT_CONVERSATION_RUNNING"
    assert (g.root / "project/result.txt").exists()
    assert g.db.get(AgentConversationBinding, item.id) is not None


def test_rollback_restores_staged_files_and_directory_record(graph):
    g = graph
    directory = g.directory("project")
    finish(g.db)
    work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    assert not (g.root / "project").exists()
    g.db.rollback()
    run_rollback_actions(g.db)
    assert (g.root / "project/result.txt").read_text() == "project"
    assert g.db.get(AgentWorkDirectory, directory["id"]) is not None


def test_cleanup_failure_is_durable_and_retry_does_not_touch_recreated_path(graph, monkeypatch):
    g = graph
    directory = g.directory("project")
    finish(g.db)
    original = directory_deletion.purge
    monkeypatch.setattr(
        directory_deletion, "purge", lambda *_: (_ for _ in ()).throw(OSError("unavailable"))
    )
    work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    finish(g.db)
    task = g.db.scalar(
        select(BackgroundTask).where(BackgroundTask.task_type == "CLEANUP_AGENT_WORK_DIRECTORY")
    )
    (g.root / "project").mkdir()
    (g.root / "project/new.txt").write_text("keep")
    monkeypatch.setattr(directory_deletion, "purge", original)
    directory_deletion.cleanup(g.db, task.aggregate_id, task.payload_json)
    assert (g.root / "project/new.txt").read_text() == "keep"
    assert not list((g.root / ".flowweave/deleted-workspaces").iterdir())


def test_symlinked_directory_is_rejected_without_touching_target(graph, tmp_path):
    g = graph
    directory = g.directory("project")
    finish(g.db)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (g.root / "project/result.txt").unlink()
    (g.root / "project").rmdir()
    (g.root / "project").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    assert (outside / "keep.txt").read_text() == "keep"
    assert g.db.get(AgentWorkDirectory, directory["id"]) is not None


def test_foreign_user_cannot_delete_archived_directory(graph):
    g = graph
    directory = g.directory("project")
    finish(g.db)
    with tenant_user(USER_USER_ID):
        with pytest.raises(DomainError):
            work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    assert (g.root / "project/result.txt").exists()


def test_child_symlink_is_unlinked_without_following_it(graph, tmp_path):
    g = graph
    directory = g.directory("project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (g.root / "project/link").symlink_to(outside, target_is_directory=True)
    finish(g.db)
    work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    finish(g.db)
    assert not (g.root / "project").exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_user_scoped_single_conversation_attachment_cleanup(graph):
    from flowweave.modules.agent_sessions.application import conversations

    g = graph
    item = g.binding()
    path, runtime_path = g.attachment(item.id)
    g.db.add(
        AgentConversationMessageAttachment(
            binding_id=item.id,
            event_id="event",
            filename="note.txt",
            mime_type="text/plain",
            byte_size=7,
            path=runtime_path,
        )
    )
    finish(g.db)
    conversations.delete_conversation(g.db, g.host.id, item.id, "single-delete")
    assert not path.exists()


def test_root_path_is_never_a_deletion_scope(graph):
    g = graph
    with pytest.raises(DomainError):
        work_directories.create_work_directory(g.db, g.host.id, "root", (".",))
    assert g.root.is_dir()


@pytest.mark.parametrize("archived", [False, True])
def test_existing_directory_delete_endpoint_removes_files_in_both_states(graph, client, archived):
    g = graph
    directory = g.directory("project")
    binding = g.binding(directory, archived=archived)
    binding_id = binding.id
    if archived:
        g.db.get(AgentWorkDirectory, directory["id"]).archived_at = datetime.now(UTC)
    finish(g.db)
    response = client.delete(
        f"/api/v1/agent-workspaces/{g.host.id}/work-directories/{directory['id']}"
    )
    assert response.status_code == 204, response.text
    assert not (g.root / "project").exists()
    g.db.expire_all()
    assert g.db.get(AgentConversationBinding, binding_id) is None


def test_cleanup_worker_restores_the_job_owner_instead_of_global_bypass(graph):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.modules.users.application.security import current_user_id

    g = graph
    directory = g.directory("project")
    finish(g.db)
    work_directories.delete_work_directory(g.db, g.host.id, directory["id"])
    finish(g.db)
    task = g.db.scalar(
        select(BackgroundTask).where(BackgroundTask.task_type == "CLEANUP_AGENT_WORK_DIRECTORY")
    )
    with tenant_user(USER_USER_ID), TaskWorker._task_tenant_context(task):
        assert current_user_id() == FLOWWEAVE_USER_ID
        directory_deletion.cleanup(g.db, task.aggregate_id, task.payload_json)


def test_legacy_shared_directory_without_path_rows_is_protected(graph):
    g = graph
    target = g.directory("target", ["shared"])
    legacy = g.directory("legacy", ["shared"])
    g.db.execute(
        delete(AgentWorkDirectoryPath).where(
            AgentWorkDirectoryPath.version_id == legacy["current_version"]["id"]
        )
    )
    finish(g.db)
    with pytest.raises(DomainError) as caught:
        work_directories.delete_work_directory(g.db, g.host.id, target["id"])
    assert caught.value.code == "AGENT_WORK_DIRECTORY_SHARED_PATH"
    assert (g.root / "shared/result.txt").exists()


def test_incomplete_multi_path_version_never_deletes_metadata_or_root(graph):
    g = graph
    target = g.directory("target", ["a", "b"])
    g.db.execute(
        delete(AgentWorkDirectoryPath).where(
            AgentWorkDirectoryPath.version_id == target["current_version"]["id"]
        )
    )
    finish(g.db)
    with pytest.raises(DomainError) as caught:
        work_directories.delete_work_directory(g.db, g.host.id, target["id"])
    assert caught.value.code == "AGENT_WORK_DIRECTORY_VERSION_MISSING"
    assert g.db.get(AgentWorkDirectory, target["id"]) is not None
    assert g.db.get(AgentWorkDirectoryVersion, target["current_version"]["id"]) is not None
    assert (g.root / "a/result.txt").exists()


def test_native_failure_restores_files_and_retry_allows_missing_native_conversation(
    graph, monkeypatch
):
    g = graph
    target = g.directory("target")
    binding = g.binding(target)
    attachment, _ = g.attachment(binding.id)
    finish(g.db)

    def unavailable(_):
        raise DomainError("RUNTIME_UNAVAILABLE", "unavailable", 503)

    monkeypatch.setattr(g.runtime, "delete_conversation", unavailable)
    with pytest.raises(DomainError):
        work_directories.delete_work_directory(g.db, g.host.id, target["id"])
    g.db.rollback()
    run_rollback_actions(g.db)
    assert (g.root / "target/result.txt").exists()
    assert attachment.exists()

    def missing(_):
        raise DomainError("RUNTIME_CONVERSATION_MISSING", "missing", 404)

    monkeypatch.setattr(g.runtime, "input_readiness", missing)
    monkeypatch.setattr(g.runtime, "delete_conversation", missing)
    work_directories.delete_work_directory(g.db, g.host.id, target["id"])
    finish(g.db)
    assert not attachment.exists()
    assert not (g.root / "target").exists()


def test_workspace_file_upload_chunks_are_removed_with_directory(graph):
    g = graph
    target = g.directory("target")
    upload = AgentAttachmentUpload(
        host_kind="AGENT_WORKSPACE",
        host_id=g.host.id,
        work_directory_id=target["id"],
        upload_kind="WORKSPACE_FILE",
        target_path="target/new.txt",
        filename="new.txt",
        mime_type="text/plain",
        total_size=3,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    g.db.add(upload)
    g.db.flush()
    upload_id = upload.id
    g.db.add(AgentAttachmentUploadPart(upload_id=upload_id, part_number=0, content=b"new"))
    finish(g.db)
    work_directories.delete_work_directory(g.db, g.host.id, target["id"])
    finish(g.db)
    assert g.db.get(AgentAttachmentUpload, upload_id) is None
    assert not list(
        g.db.scalars(
            select(AgentAttachmentUploadPart).where(
                AgentAttachmentUploadPart.upload_id == upload_id
            )
        )
    )
