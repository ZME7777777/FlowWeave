from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from flowweave.bootstrap.runtime_provider import RuntimeProviderResourceWrite
from flowweave.modules.agent_workspaces.application import (
    conversations,
    titles,
    work_directories,
    workspace,
)
from flowweave.modules.agent_workspaces.application.service import (
    ensure_default_agent_workspace,
    process_agent_workspace_runtime,
    recover_default_agent_workspace_runtime_task,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCommand,
    AgentWorkDirectory,
    AgentWorkDirectoryPath,
    AgentWorkDirectoryVersion,
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.agent_workspaces.presentation import router as agent_router
from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.model_providers.public import TitleProviderSnapshot
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.modules.tasks.public import Lease
from flowweave.runtime.base import (
    RuntimeConversationIdentity,
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimePendingAction,
    RuntimePendingConfirmation,
    RuntimeProvider,
    RuntimeResult,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, ManagedSandbox, TaskState
from flowweave.shared.settings import settings_context


def _agent_project_root(settings, db, workspace):
    allocation = db.scalar(
        select(AgentWorkspaceRuntimeAllocation).where(
            AgentWorkspaceRuntimeAllocation.workspace_id == workspace.id
        )
    )
    assert allocation is not None
    return settings.workspace_root / allocation.relative_root / "workspace/project"


def test_agent_conversation_stream_closes_idle_upstream_on_websocket_disconnect(
    settings, monkeypatch
):
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()

    class Runtime:
        async def stream_events(self, _handle):
            stream_started.set()
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                stream_closed.set()

    class Database:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run_sync(self, callback):
            return callback(self)

    class Container:
        def __init__(self) -> None:
            self.settings = settings
            self.database = Database()
            self.runtime = Runtime()

    class WebSocket:
        accepted = False

        async def accept(self) -> None:
            self.accepted = True

        async def receive(self) -> dict[str, str]:
            await stream_started.wait()
            return {"type": "websocket.disconnect"}

        async def send_json(self, _event) -> None:
            raise AssertionError("an idle stream must not send an event")

        async def close(self, **_kwargs) -> None:
            raise AssertionError("a normal disconnect must not be reported as an error")

    runtime = Runtime()
    container = Container()
    container.runtime = runtime
    websocket = WebSocket()
    monkeypatch.setattr(
        agent_router.conversations,
        "runtime_stream_details",
        lambda *_args: ("mock", object()),
    )
    monkeypatch.setattr(agent_router, "runtime_for", lambda *_args: runtime)

    asyncio.run(
        agent_router.agent_conversation_stream(
            websocket,
            "workspace-1",
            "binding-1",
            container,
        )
    )

    assert websocket.accepted is True
    assert stream_closed.is_set()


def test_agent_work_directory_root_is_implicit_and_versions_are_immutable(
    settings, db_session_factory
):
    with settings_context(settings), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        project_root = _agent_project_root(settings, db, workspace)
        (project_root / "service-a").mkdir()
        (project_root / "service-b").mkdir()
        (project_root / "service-c").mkdir()

        initial = work_directories.list_work_directories(db, workspace.id)
        assert initial == {
            "root": {
                "kind": "ROOT",
                "display_name": "根工作区",
                "working_directory": "/runtime/workspace/project",
            },
            "items": [],
        }
        assert db.scalar(select(AgentWorkDirectory.id)) is None

        created = work_directories.create_work_directory(
            db, workspace.id, "后端服务", ("service-a",)
        )
        directory_id = created["id"]
        first_version_id = created["current_version"]["id"]
        assert created["current_version"] == {
            "id": first_version_id,
            "version": 1,
            "selected_paths": ["service-a"],
            "working_directory": "/runtime/workspace/project/service-a",
        }

        multiple = work_directories.create_work_directory(
            db, workspace.id, "跨服务", ("service-b", "service-c")
        )
        assert multiple["current_version"]["selected_paths"] == ["service-b", "service-c"]
        assert multiple["current_version"]["working_directory"] == "/runtime/workspace/project"

        updated = work_directories.update_work_directory(
            db,
            workspace.id,
            directory_id,
            display_name=None,
            selected_paths=("service-b", "service-c"),
        )
        second_version_id = updated["current_version"]["id"]
        assert updated["current_version"]["version"] == 2
        assert updated["current_version"]["working_directory"] == "/runtime/workspace/project"
        assert second_version_id != first_version_id

        renamed = work_directories.update_work_directory(
            db,
            workspace.id,
            directory_id,
            display_name="核心后端",
            selected_paths=None,
        )
        assert renamed["display_name"] == "核心后端"
        assert renamed["current_version"]["version"] == 2
        versions = list(
            db.scalars(
                select(AgentWorkDirectoryVersion)
                .where(AgentWorkDirectoryVersion.work_directory_id == directory_id)
                .order_by(AgentWorkDirectoryVersion.version)
            )
        )
        assert [(item.id, item.version, item.working_path) for item in versions] == [
            (first_version_id, 1, "service-a"),
            (second_version_id, 2, "."),
        ]
        assert list(
            db.scalars(
                select(AgentWorkDirectoryPath.relative_path)
                .where(AgentWorkDirectoryPath.version_id == first_version_id)
                .order_by(AgentWorkDirectoryPath.position)
            )
        ) == ["service-a"]

        work_directories.archive_work_directory(db, workspace.id, directory_id)
        active = work_directories.list_work_directories(db, workspace.id)
        assert [item["id"] for item in active["items"]] == [multiple["id"]]
        archived = work_directories.get_work_directory(db, workspace.id, directory_id)
        assert archived["state"] == "ARCHIVED"
        assert archived["current_version"]["id"] == second_version_id


@pytest.mark.parametrize(
    ("selected_paths", "error_code"),
    [
        (("/absolute",), "AGENT_WORK_DIRECTORY_PATH_INVALID"),
        (("../escape",), "AGENT_WORK_DIRECTORY_PATH_INVALID"),
        (("ordinary\\child",), "AGENT_WORK_DIRECTORY_PATH_INVALID"),
        (("a" * 501,), "AGENT_WORK_DIRECTORY_PATH_INVALID"),
        (("ordinary", "ordinary"), "AGENT_WORK_DIRECTORY_PATH_DUPLICATE"),
        (("parent", "parent/child"), "AGENT_WORK_DIRECTORY_PATH_OVERLAP"),
        (("missing",), "AGENT_WORK_DIRECTORY_PATH_NOT_FOUND"),
        (("file.txt",), "AGENT_WORK_DIRECTORY_PATH_NOT_DIRECTORY"),
        (("link",), "AGENT_WORK_DIRECTORY_PATH_NOT_DIRECTORY"),
    ],
)
def test_agent_work_directory_rejects_unsafe_or_invalid_paths(
    settings, db_session_factory, selected_paths, error_code
):
    with settings_context(settings), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        project_root = _agent_project_root(settings, db, workspace)
        (project_root / "ordinary").mkdir()
        (project_root / "parent/child").mkdir(parents=True)
        (project_root / "file.txt").write_text("not a directory")
        (project_root / "link").symlink_to(project_root / "ordinary", target_is_directory=True)

        with pytest.raises(DomainError) as raised:
            work_directories.create_work_directory(db, workspace.id, "不安全目录", selected_paths)
        assert raised.value.code == error_code
        assert raised.value.status == 422


def test_agent_work_directory_http_crud_and_empty_patch(client, settings, db_session_factory):
    with settings_context(settings), db_session_factory() as db:
        ensure_default_agent_workspace(db)
        db.commit()

    workspace_response = client.get("/api/v1/agent-workspaces/default")
    assert workspace_response.status_code == 200
    workspace_id = workspace_response.json()["id"]
    project_root = settings.workspace_root / ".agent-workspaces/platform-default/workspace/project"
    (project_root / "web").mkdir()

    listed = client.get(f"/api/v1/agent-workspaces/{workspace_id}/work-directories")
    assert listed.status_code == 200
    assert listed.json()["root"]["working_directory"] == "/runtime/workspace/project"
    assert listed.json()["items"] == []

    created = client.post(
        f"/api/v1/agent-workspaces/{workspace_id}/work-directories",
        json={"display_name": "Web", "selected_paths": ["web"]},
    )
    assert created.status_code == 201, created.text
    work_directory_id = created.json()["id"]

    empty_patch = client.patch(
        f"/api/v1/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}",
        json={},
    )
    assert empty_patch.status_code == 422
    assert empty_patch.json()["error"]["code"] == "AGENT_WORK_DIRECTORY_PATCH_EMPTY"

    renamed = client.patch(
        f"/api/v1/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}",
        json={"display_name": "Web 前端"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Web 前端"

    fetched = client.get(
        f"/api/v1/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}"
    )
    assert fetched.status_code == 200
    assert fetched.json()["current_version"]["selected_paths"] == ["web"]

    archived = client.delete(
        f"/api/v1/agent-workspaces/{workspace_id}/work-directories/{work_directory_id}"
    )
    assert archived.status_code == 204
    listed = client.get(f"/api/v1/agent-workspaces/{workspace_id}/work-directories")
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_default_agent_workspace_has_external_storage_and_no_flow_owner(
    settings, db_session_factory
):
    with settings_context(settings), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        allocation = db.scalar(
            select(AgentWorkspaceRuntimeAllocation).where(
                AgentWorkspaceRuntimeAllocation.workspace_id == workspace.id
            )
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert allocation is not None
        assert runtime is not None
        assert allocation.relative_root == ".agent-workspaces/platform-default"
        assert (settings.workspace_root / allocation.relative_root / "workspace/project").is_dir()
        assert (settings.workspace_root / allocation.relative_root / "state/conversations").is_dir()
        assert runtime.runtime_image_digest == "sha256:" + "0" * 64
        db.commit()


def test_unavailable_workspace_runtime_refreshes_missing_deployment_image(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"runtime_adapter": "openhands"})
    first_digest = "sha256:" + "1" * 64
    replacement_digest = "sha256:" + "2" * 64
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
        lambda _reference: ("image", first_digest),
    )
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert runtime is not None and runtime.runtime_image_digest == first_digest
        monkeypatch.setattr(
            "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
            lambda _reference: ("image", replacement_digest),
        )
        runtime.active_generation = 1
        runtime.status = "RECONNECTING"
        ensure_default_agent_workspace(db)
        assert runtime.runtime_image_digest == replacement_digest
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == workspace.id))
        assert task is not None
        task.state = TaskState.DEAD
        task.attempts = task.max_attempts
        task.last_error = "previous deployment image was unavailable"
        ensure_default_agent_workspace(db)
        assert task.state == TaskState.RETRY
        assert task.attempts == 0
        assert task.last_error is None


def test_workspace_runtime_keeps_old_image_only_for_provider_confirmed_resource(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(
        update={"runtime_adapter": "openhands", "terminal_environment_backend": "docker"}
    )
    old_digest = "sha256:" + "4" * 64
    replacement_digest = "sha256:" + "5" * 64
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
        lambda _reference: ("image", old_digest),
    )
    with settings_context(configured), db_session_factory() as db:
        workspace_item = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(
                AgentWorkspaceRuntime.workspace_id == workspace_item.id
            )
        )
        assert runtime is not None
        runtime.active_generation = 1
        runtime.status = "RECONNECTING"
        db.add(
            ManagedSandbox(
                kind="AGENT_RUNTIME",
                owner_type="AGENT_WORKSPACE",
                owner_id=workspace_item.id,
                backend_resource_name=f"agent-workspace-{workspace_item.id}",
                desired_state="RUNNING",
                observed_state="CREATING",
                backend_resource_id="",
                generation=1,
                image_reference=old_digest,
                agent_workspace_allocation_id=runtime.workspace_allocation_id,
                hard_expires_at=datetime.max.replace(tzinfo=UTC),
            )
        )
        db.flush()
        monkeypatch.setattr(
            "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
            lambda _reference: ("image", replacement_digest),
        )

        ensure_default_agent_workspace(db)

        assert runtime.runtime_image_digest == replacement_digest
        assert runtime.status == "STARTING"


def test_dead_workspace_runtime_task_retries_when_resource_is_not_healthy(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(
        update={"runtime_adapter": "openhands", "terminal_environment_backend": "docker"}
    )
    digest = "sha256:" + "3" * 64
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
        lambda _reference: ("image", digest),
    )
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == workspace.id))
        assert runtime is not None and task is not None

        runtime.active_generation = 1
        runtime.status = "DEGRADED"
        db.add(
            ManagedSandbox(
                kind="AGENT_RUNTIME",
                owner_type="AGENT_WORKSPACE",
                owner_id=workspace.id,
                backend_resource_name=f"agent-workspace-{workspace.id}",
                desired_state="RUNNING",
                observed_state="ERROR",
                generation=1,
                image_reference=digest,
                agent_workspace_allocation_id=runtime.workspace_allocation_id,
                hard_expires_at=datetime.max.replace(tzinfo=UTC),
            )
        )
        task.state = TaskState.SUCCEEDED
        recovery_task = BackgroundTask(
            task_type="PROVISION_AGENT_WORKSPACE_RUNTIME",
            aggregate_type="AGENT_WORKSPACE",
            aggregate_id=workspace.id,
            idempotency_key=f"recover-agent-workspace-runtime:{workspace.id}:1",
            state=TaskState.DEAD,
            attempts=20,
            max_attempts=20,
            last_error="Docker backend unavailable",
        )
        db.add(recovery_task)
        db.flush()

        assert recover_default_agent_workspace_runtime_task(db) is True

        assert recovery_task.state == TaskState.RETRY
        assert recovery_task.attempts == 0
        assert recovery_task.last_error is None


def test_workspace_runtime_replaces_deleted_physical_generation(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"terminal_environment_backend": "docker"})

    def ensure_running(_self, resource, *, runtime_secret_key):
        assert resource.owner_type == "AGENT_WORKSPACE"
        assert runtime_secret_key is not None
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier=f"container-{resource.generation}",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda *_args, **_kwargs: None)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        process_agent_workspace_runtime(db, workspace.id)
        first = db.scalar(
            select(ManagedSandbox).where(
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == workspace.id,
            )
        )
        assert first is not None
        assert first.generation == 1
        first.desired_state = "DELETED"
        first.observed_state = "ERROR"
        first.next_reconcile_at = datetime.now(UTC)
        db.flush()

        process_agent_workspace_runtime(db, workspace.id)
        current = db.scalar(
            select(ManagedSandbox)
            .where(
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == workspace.id,
            )
            .order_by(ManagedSandbox.generation.desc())
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        generations = list(
            db.scalars(
                select(AgentWorkspaceRuntimeGeneration).order_by(
                    AgentWorkspaceRuntimeGeneration.generation
                )
            )
        )
        assert current is not None
        assert current.generation == 2
        assert runtime is not None and runtime.active_generation == 2
        assert [item.generation for item in generations] == [1, 2]


def test_workspace_runtime_recovery_refreshes_removed_image_digest(
    settings, db_session_factory, monkeypatch
):
    old_digest = "sha256:" + "6" * 64
    current_digest = "sha256:" + "7" * 64
    configured = settings.model_copy(
        update={
            "runtime_adapter": "openhands",
            "terminal_environment_backend": "docker",
        }
    )
    resolved_digest = old_digest
    captured: list[ManagedSandbox] = []

    def resolve_image(_reference):
        return "image", resolved_digest

    def ensure_running(_self, resource, *, runtime_secret_key):
        captured.append(resource)
        assert runtime_secret_key is not None
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="replacement-container",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
        resolve_image,
    )
    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert runtime is not None and runtime.runtime_image_digest == old_digest
        runtime.status = "RECONNECTING"
        runtime.active_generation = 13

        resolved_digest = current_digest
        process_agent_workspace_runtime(db, workspace.id)

        assert len(captured) == 1
        assert captured[0].generation == 1
        assert captured[0].image_reference == current_digest
        assert runtime.runtime_image_digest == current_digest
        assert runtime.status == "ACTIVE"
        assert runtime.active_generation == 1


def test_agent_workspace_runtime_spec_matches_runtime_provider_contract(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"terminal_environment_backend": "docker"})
    captured: list[ManagedSandbox] = []

    def ensure_running(_self, resource, *, runtime_secret_key):
        captured.append(resource)
        assert runtime_secret_key is not None
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="agent-workspace-container",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        process_agent_workspace_runtime(db, workspace.id)
        assert len(captured) == 1
        resource = captured[0]
        payload = RuntimeProviderResourceWrite.model_validate(
            {
                "manager_scope": configured.sandbox_manager_scope,
                "id": resource.id,
                "kind": resource.kind,
                "owner_type": resource.owner_type,
                "owner_id": resource.owner_id,
                "backend_resource_name": resource.backend_resource_name,
                "image_reference": resource.image_reference,
                "spec": resource.spec_json,
                "created_at": resource.created_at,
                "runtime_secret_key": "x" * 32,
            }
        )
        assert str(payload.spec.runtime_allocation_id) == resource.agent_workspace_allocation_id


def _ready_workspace_for_conversation(db):
    workspace = ensure_default_agent_workspace(db)
    provider = ModelProvider(
        name=f"agent-workspace-provider-{workspace.id}",
        base_url="https://models.example.test/v1",
        connection_state="CONNECTED",
    )
    db.add(provider)
    db.flush()
    db.add(
        ProviderModel(
            provider_id=provider.id,
            model_name="test-model",
            enabled=True,
            is_default=True,
        )
    )
    workspace.default_model_provider_id = provider.id
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    assert runtime is not None
    runtime.status = "ACTIVE"
    runtime.active_generation = 1
    db.add(
        ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="AGENT_WORKSPACE",
            owner_id=workspace.id,
            backend_resource_name=f"agent-workspace-{workspace.id}",
            backend_resource_id=f"agent-workspace-container-{workspace.id}",
            image_reference=runtime.runtime_image_digest,
            agent_workspace_allocation_id=runtime.workspace_allocation_id,
            hard_expires_at=datetime.max.replace(tzinfo=UTC),
            observed_state="READY",
        )
    )
    db.flush()
    return workspace


def test_agent_workspace_conversation_create_is_idempotent_and_uses_external_identity(
    settings, db_session_factory, monkeypatch
):
    class CapturingRuntime(MockRuntime):
        request = None

        def create_conversation(self, request):
            self.request = request
            return super().create_conversation(request)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **_kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=_kwargs.get("model_name") or "test-model",
            api_key="x",
            reasoning_effort=_kwargs.get("reasoning_effort"),
        ),
    )
    runtime = CapturingRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        first = conversations.create_conversation(
            db, workspace.id, "第一会话", workspace.default_model_provider_id, "create-key"
        )
        replay = conversations.create_conversation(
            db, workspace.id, "ignored", workspace.default_model_provider_id, "create-key"
        )

        assert replay == first
        assert first["lifecycle"] == "ACTIVE"
        assert first["display_title"] == "第一会话"
        assert len(conversations.list_conversations(db, workspace.id)) == 1
        assert runtime.request is not None
        assert runtime.request.agent_spec.confirmation_policy == "NEVER"
        assert runtime.request.agent_spec.condenser.kind == "LLM_SUMMARIZING"
        assert runtime.request.agent_spec.condenser_provider is not None
        assert runtime.request.workspace_ref == "/runtime/workspace/project"
        assert runtime.request.agent_spec.agent_context.system_message_suffix == (
            "当前会话的项目根目录是 /runtime/workspace/project。\n"
            "所有需要保留的代码、配置、文档和用户产物必须写入该目录或其子目录。\n"
            "可按需求或功能自行创建子目录；优先使用相对于项目根的路径。\n"
            "不要将用户项目文件写入项目根以外的位置，例如 /runtime 的其他目录、/tmp 或 HOME。\n"
            "不要向用户解释宿主机路径、Docker 挂载或容器实现细节；对用户而言，这就是项目根目录。"
        )


def test_agent_workspace_files_and_terminal_revalidate_draft_directory(
    settings, db_session_factory
):
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        item = _ready_workspace_for_conversation(db)
        project_root = _agent_project_root(settings, db, item)
        (project_root / "backend").mkdir()
        (project_root / "backend/src").mkdir()
        (project_root / "backend/README.md").write_bytes(b"mock workspace file\n")
        directory = work_directories.create_work_directory(db, item.id, "后端", ("backend",))

        details = workspace.details(db, item.id, work_directory_id=directory["id"])
        assert details["working_directory"] == "/runtime/workspace/project/backend"
        assert details["ide"]["gateway"]["supported"] is False
        assert details["files"] == [
            {
                "path": "/runtime/workspace/project/backend/src",
                "kind": "directory",
                "size": 0,
            },
            {
                "path": "/runtime/workspace/project/backend/README.md",
                "kind": "file",
                "size": 20,
            },
        ]
        downloaded = workspace.download(
            db,
            item.id,
            "/runtime/workspace/project/backend/README.md",
            work_directory_id=directory["id"],
        )
        assert downloaded.content == b"mock workspace file\n"
        with pytest.raises(DomainError, match="当前工作目录范围"):
            workspace.download(
                db,
                item.id,
                "/runtime/workspace/project/README.md",
                work_directory_id=directory["id"],
            )
        resource_name, resource_id, working_directory, _ = workspace.terminal_details(
            db, item.id, work_directory_id=directory["id"]
        )
        assert resource_name == f"agent-workspace-{item.id}"
        assert resource_id
        assert working_directory == "/runtime/workspace/project/backend"

        work_directories.archive_work_directory(db, item.id, directory["id"])
        with pytest.raises(DomainError) as raised:
            workspace.details(db, item.id, work_directory_id=directory["id"])
        assert raised.value.code == "AGENT_WORK_DIRECTORY_ARCHIVED"


def test_agent_workspace_allows_only_bound_conversation_attachments_outside_scope(
    settings, db_session_factory, monkeypatch
):
    """An upload may be outside a frozen directory, but not outside its conversation."""

    class UploadRuntime(MockRuntime):
        def upload_workspace_file(self, handle, *, filename, content_type, content):
            del handle, content_type, content
            return f"/runtime/workspace/project/uploads/{'a' * 32}-{filename}"

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **_kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="test-key",
        ),
    )

    with settings_context(settings), db_session_factory() as db, runtime_context(UploadRuntime()):
        item = _ready_workspace_for_conversation(db)
        project_root = _agent_project_root(settings, db, item)
        (project_root / "service").mkdir()
        (project_root / "service" / "README.md").write_text("scoped")
        directory = work_directories.create_work_directory(db, item.id, "服务", ("service",))
        owner = conversations.create_conversation(
            db, item.id, None, item.default_model_provider_id, "attachment-owner"
        )
        other = conversations.create_conversation(
            db, item.id, None, item.default_model_provider_id, "attachment-other"
        )
        # The work-directory is frozen on the binding.  Make both test
        # conversations use the restricted scope so `uploads/` is genuinely
        # outside their normal readable tree.
        for binding_id in (owner["id"], other["id"]):
            binding = db.get(AgentConversationBinding, binding_id)
            assert binding is not None
            binding.work_directory_version_id = directory["current_version"]["id"]
            binding.working_directory = "/runtime/workspace/project/service"
        db.flush()
        attachment = conversations.upload_attachment(
            db, item.id, owner["id"], filename="requirements.pdf",
            content_type="application/pdf", content=b"%PDF-1.7",
        )
        upload_path = project_root / "uploads" / f"{'a' * 32}-requirements.pdf"
        upload_path.parent.mkdir()
        upload_path.write_bytes(b"%PDF-1.7")
        # A bound upload is previewable from the right-hand panel before it is
        # sent, but remains inaccessible to every other conversation.
        assert workspace.download(
            db, item.id, str(attachment["path"]), binding_id=owner["id"]
        ).content == b"%PDF-1.7"
        with pytest.raises(DomainError, match="当前工作目录范围"):
            workspace.download(
                db, item.id, str(attachment["path"]), binding_id=other["id"]
            )
        conversations.message(
            db, item.id, owner["id"], "", ({"path": str(attachment["path"]), "filename": "requirements.pdf", "mime_type": "application/pdf", "byte_size": 8},)
        )

        details = workspace.details(db, item.id, binding_id=owner["id"])
        assert str(attachment["path"]) in {entry["path"] for entry in details["files"]}
        assert workspace.download(
            db, item.id, str(attachment["path"]), binding_id=owner["id"]
        ).content == b"%PDF-1.7"
        with pytest.raises(DomainError, match="当前工作目录范围"):
            workspace.download(
                db, item.id, str(attachment["path"]), binding_id=other["id"]
            )


def test_agent_workspace_multi_directory_files_are_scoped_and_frozen(settings, db_session_factory):
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        item = _ready_workspace_for_conversation(db)
        project_root = _agent_project_root(settings, db, item)
        for name in ("service-a", "service-b", "service-c"):
            (project_root / name).mkdir()
            (project_root / name / f"{name}.txt").write_text(name)
        (project_root / "outside.txt").write_text("outside")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=project_root / "service-a",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "service-a.txt"],
            cwd=project_root / "service-a",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=FlowWeave Test",
                "-c",
                "user.email=flowweave@example.test",
                "commit",
                "-m",
                "initial",
            ],
            cwd=project_root / "service-a",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "ssh://git.example.test/product.git"],
            cwd=project_root / "service-a",
            check=True,
            capture_output=True,
        )

        directory = work_directories.create_work_directory(
            db, item.id, "跨服务", ("service-a", "service-b")
        )
        details = workspace.details(db, item.id, work_directory_id=directory["id"])
        assert details["scope"] == {
            "kind": "WORK_DIRECTORY",
            "id": directory["id"],
            "display_name": "跨服务",
        }
        assert details["working_directory"] == "/runtime/workspace/project"
        visible_paths = {entry["path"] for entry in details["files"]}
        assert visible_paths == {
            "/runtime/workspace/project/service-a",
            "/runtime/workspace/project/service-a/service-a.txt",
            "/runtime/workspace/project/service-b",
            "/runtime/workspace/project/service-b/service-b.txt",
        }
        assert details["repositories"][0]["branch"] == "main"
        assert len(details["repositories"][0]["head"]) == 40
        assert details["repositories"][0]["remote"] == ("ssh://git.example.test/product.git")
        _, _, terminal_directory, _ = workspace.terminal_details(
            db, item.id, work_directory_id=directory["id"]
        )
        assert terminal_directory == "/runtime/workspace/project"
        assert (
            workspace.download(
                db,
                item.id,
                "/runtime/workspace/project/service-a/service-a.txt",
                work_directory_id=directory["id"],
            ).content
            == b"service-a"
        )
        for denied in (
            "/runtime/workspace/project/service-c/service-c.txt",
            "/runtime/workspace/project/outside.txt",
        ):
            with pytest.raises(DomainError, match="当前工作目录范围"):
                workspace.download(db, item.id, denied, work_directory_id=directory["id"])

        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == item.id)
        )
        assert runtime is not None
        binding = AgentConversationBinding(
            workspace_id=item.id,
            runtime_session_id=runtime.id,
            work_directory_version_id=directory["current_version"]["id"],
            working_directory="/runtime/workspace/project",
            openhands_conversation_id="11111111-1111-4111-8111-111111111111",
            create_idempotency_key="frozen-multi-directory",
            lifecycle="ACTIVE",
        )
        db.add(binding)
        db.flush()
        work_directories.update_work_directory(
            db, item.id, directory["id"], display_name=None, selected_paths=("service-c",)
        )
        frozen = workspace.details(db, item.id, binding_id=binding.id)
        frozen_paths = {entry["path"] for entry in frozen["files"]}
        assert "/runtime/workspace/project/service-a/service-a.txt" in frozen_paths
        assert "/runtime/workspace/project/service-b/service-b.txt" in frozen_paths
        assert "/runtime/workspace/project/service-c/service-c.txt" not in frozen_paths
        with pytest.raises(DomainError, match="当前工作目录范围"):
            workspace.download(
                db,
                item.id,
                "/runtime/workspace/project/service-c/service-c.txt",
                binding_id=binding.id,
            )


def test_agent_workspace_bootstrap_creates_only_on_first_message_and_freezes_directory(
    settings, db_session_factory, monkeypatch
):
    class BootstrapRuntime(MockRuntime):
        request = None
        sent: list[str] = []
        created = 0
        initial_sent = False

        def create_conversation(self, request):
            self.request = request
            self.created += 1
            return super().create_conversation(request)

        def reload_conversation(self, handle, *, expected=None):
            del expected
            assert self.request is not None
            return RuntimeConversationIdentity(
                conversation_id=handle.conversation_id,
                workspace_working_dir=self.request.workspace_ref,
                persistence_dir=(
                    f"/runtime/state/conversations/{handle.conversation_id.replace('-', '')}"
                ),
                event_id=None,
                parent_id=None,
                action_id=None,
                tool_call_id=None,
            )

        def send_message(self, handle, content, image_urls=()):
            del handle, image_urls
            self.sent.append(content)
            self.initial_sent = True
            return RuntimeResult(status="RUNNING", cursor="first-user-event")

        def read_active_events(self, handle):
            del handle
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        cursor="first-user-event",
                        event_type="MESSAGE",
                        payload={"source": "user", "parent_id": "__root__"},
                    ),
                )
                if self.initial_sent
                else ()
            )

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
            reasoning_effort=kwargs.get("reasoning_effort"),
        ),
    )
    runtime = BootstrapRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        project_root = _agent_project_root(settings, db, workspace)
        (project_root / "backend").mkdir()
        directory = work_directories.create_work_directory(db, workspace.id, "后端", ("backend",))
        assert conversations.list_conversations(db, workspace.id) == []

        first = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=directory["id"],
            model_provider_id=workspace.default_model_provider_id,
            content="实现接口",
            idempotency_key="draft-001",
        )
        replay = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="不得重复发送",
            idempotency_key="draft-001",
        )

        assert replay == first
        assert runtime.created == 1
        assert runtime.sent == ["实现接口"]
        assert runtime.request is not None
        assert runtime.request.workspace_ref == "/runtime/workspace/project/backend"
        assert first["cursor"] == "first-user-event"
        assert (
            first["conversation"]["work_directory_version_id"] == directory["current_version"]["id"]
        )
        assert first["conversation"]["work_directory_id"] == directory["id"]
        assert first["conversation"]["working_directory"] == "/runtime/workspace/project/backend"
        assert first["conversation"]["display_title"] == "实现接口"
        assert first["conversation"]["title_state"] == "PENDING"
        title_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "GENERATE_AGENT_CONVERSATION_TITLE"
            )
        )
        assert title_task is not None
        assert title_task.aggregate_id == first["conversation"]["id"]
        assert title_task.payload_json["first_message"] == "实现接口"
        listed = conversations.list_conversations(db, workspace.id)
        assert listed[0]["work_directory_id"] == directory["id"]
        assert len(conversations.list_conversations(db, workspace.id)) == 1


def _title_task_lease(task: BackgroundTask) -> Lease:
    task.state = TaskState.RUNNING
    task.lease_owner = "title-test-worker"
    task.lease_generation = 1
    task.lease_until = datetime.max.replace(tzinfo=UTC)
    return Lease(task.id, task.lease_owner, task.lease_generation)


def test_agent_workspace_title_task_uses_independent_provider_and_redacts_seed(
    settings, db_session_factory, monkeypatch
):
    class BootstrapRuntime(MockRuntime):
        sent = 0

        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            self.sent += 1
            return RuntimeResult(status="RUNNING", cursor="first-user-event")

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    monkeypatch.setattr(
        titles,
        "title_provider_snapshot",
        lambda *_args: TitleProviderSnapshot(
            "https://titles.example.test/v1",
            {"Authorization": "Bearer x"},
            "title-model",
            "CHAT_COMPLETIONS",
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        titles,
        "generate_title",
        lambda _snapshot, prompt: calls.append(prompt) or "实现 Agent 工作区文件映射",
    )
    runtime = BootstrapRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="请实现 Agent 工作区的文件映射，并支持 IDEA 连接。",
            idempotency_key="title-task-001",
        )
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "GENERATE_AGENT_CONVERSATION_TITLE"
            )
        )
        assert task is not None
        titles.process_agent_conversation_title(
            db, created["conversation"]["id"], task.payload_json, _title_task_lease(task)
        )
        binding = db.get(AgentConversationBinding, created["conversation"]["id"])
        assert binding is not None
        assert binding.display_title == "实现 Agent 工作区文件映射"
        assert binding.title_state == "GENERATED"
        assert calls == ["请实现 Agent 工作区的文件映射，并支持 IDEA 连接。"]
        assert runtime.sent == 1
        assert task.payload_json == {"title_generation": 1}


def test_agent_workspace_manual_title_wins_over_late_title_task(
    settings, db_session_factory, monkeypatch
):
    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    called = False

    def unexpected_title(*_args):
        nonlocal called
        called = True
        return "自动标题"

    monkeypatch.setattr(titles, "generate_title", unexpected_title)
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="自动标题不应该覆盖手动名称",
            idempotency_key="title-task-manual",
        )
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "GENERATE_AGENT_CONVERSATION_TITLE"
            )
        )
        assert task is not None
        renamed = conversations.patch_conversation(
            db, workspace.id, created["conversation"]["id"], "我的手动会话名"
        )
        assert renamed["title_state"] == "MANUAL"
        titles.process_agent_conversation_title(
            db, created["conversation"]["id"], task.payload_json, _title_task_lease(task)
        )
        binding = db.get(AgentConversationBinding, created["conversation"]["id"])
        assert binding is not None
        assert binding.display_title == "我的手动会话名"
        assert binding.title_state == "MANUAL"
        assert binding.title_generation == 2
        assert called is False
        assert task.payload_json == {"title_generation": 1}


def test_agent_workspace_title_task_falls_back_to_normalized_first_sentence(
    settings, db_session_factory, monkeypatch
):
    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    monkeypatch.setattr(
        titles,
        "title_provider_snapshot",
        lambda *_args: (_ for _ in ()).throw(ValueError("offline")),
    )
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="  修复会话标题生成的失败兜底。\n后续补充信息  ",
            idempotency_key="title-task-fallback",
        )
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "GENERATE_AGENT_CONVERSATION_TITLE"
            )
        )
        assert task is not None
        titles.process_agent_conversation_title(
            db, created["conversation"]["id"], task.payload_json, _title_task_lease(task)
        )
        binding = db.get(AgentConversationBinding, created["conversation"]["id"])
        assert binding is not None
        assert binding.display_title == "修复会话标题生成的失败兜底。"
        assert binding.title_state == "FALLBACK"
        assert "未命名会话" not in binding.display_title


def test_agent_workspace_title_task_rejects_mechanical_generated_name(
    settings, db_session_factory, monkeypatch
):
    assert conversations.normalized_first_sentence("新会话2") == "关于“新会话2”的请求"
    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    monkeypatch.setattr(titles, "generate_title", lambda *_args: "新会话2")
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="排查支付回调重复入账",
            idempotency_key="title-task-mechanical",
        )
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "GENERATE_AGENT_CONVERSATION_TITLE"
            )
        )
        assert task is not None
        titles.process_agent_conversation_title(
            db, created["conversation"]["id"], task.payload_json, _title_task_lease(task)
        )
        binding = db.get(AgentConversationBinding, created["conversation"]["id"])
        assert binding is not None
        assert binding.display_title == "排查支付回调重复入账"
        assert binding.title_state == "FALLBACK"


def test_agent_workspace_terminal_session_names_are_safe_and_instance_scoped():
    first = workspace.terminal_session_name(
        "workspace-id",
        "sha256:ABCDEF0123456789",
        "11111111-1111-4111-8111-111111111111",
    )
    second = workspace.terminal_session_name(
        "workspace-id",
        "sha256:ABCDEF0123456789",
        "22222222-2222-4222-8222-222222222222",
    )

    assert first != second
    assert len(first) <= 64
    assert len(second) <= 64
    assert first.startswith("fw-agent-abcdef012345-")
    assert all(
        character.islower() or character.isdigit() or character == "-" for character in first
    )


def test_agent_workspace_bootstrap_explicit_failure_cleans_up_hidden_conversation(
    settings, db_session_factory, monkeypatch
):
    class FailingBootstrapRuntime(MockRuntime):
        deleted = 0

        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            raise DomainError("AGENT_INPUT_REJECTED", "invalid first input", 422)

        def delete_conversation(self, handle):
            del handle
            self.deleted += 1

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    runtime = FailingBootstrapRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        with pytest.raises(DomainError, match="invalid first input"):
            conversations.bootstrap_conversation(
                db,
                workspace.id,
                work_directory_id=None,
                model_provider_id=workspace.default_model_provider_id,
                content="会失败",
                idempotency_key="draft-known-failure",
            )
        assert db.scalar(select(AgentConversationBinding)) is None
        assert db.scalar(select(AgentConversationCommand)) is None
        assert runtime.deleted == 1
        assert conversations.list_conversations(db, workspace.id) == []


def test_agent_workspace_bootstrap_ambiguous_delivery_never_resends(
    settings, db_session_factory, monkeypatch
):
    class AmbiguousBootstrapRuntime(MockRuntime):
        sent = 0

        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            self.sent += 1
            raise DomainError("EXECUTOR_UNAVAILABLE", "temporary network failure", 503)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    runtime = AmbiguousBootstrapRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        for content in ("首条消息", "不得重发"):
            with pytest.raises(DomainError) as raised:
                conversations.bootstrap_conversation(
                    db,
                    workspace.id,
                    work_directory_id=None,
                    model_provider_id=workspace.default_model_provider_id,
                    content=content,
                    idempotency_key="draft-ambiguous",
                )
            assert raised.value.code == "AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS"
        command = db.scalar(select(AgentConversationCommand))
        assert command is not None and command.state == "AMBIGUOUS"
        assert runtime.sent == 1
        assert conversations.list_conversations(db, workspace.id) == []


def test_agent_workspace_bootstrap_reconciles_ambiguous_delivery_by_native_event_identity(
    settings, db_session_factory, monkeypatch
):
    class ReconciledBootstrapRuntime(MockRuntime):
        sent = 0

        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            self.sent += 1
            raise DomainError("EXECUTOR_UNAVAILABLE", "response lost", 503)

        def read_active_events(self, handle):
            del handle
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        cursor="native-first-user",
                        event_type="MESSAGE",
                        payload={"source": "user", "parent_id": "__root__"},
                    ),
                )
            )

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    runtime = ReconciledBootstrapRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        result = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="首条消息",
            idempotency_key="draft-reconciled",
        )
        assert result["cursor"] == "native-first-user"
        assert result["conversation"]["lifecycle"] == "ACTIVE"
        assert runtime.sent == 1


def test_agent_workspace_bootstrap_retries_ambiguous_creation_with_original_uuid(
    settings, db_session_factory, monkeypatch
):
    class CreationRetryRuntime(MockRuntime):
        created = 0
        sent = 0

        def create_conversation(self, request):
            self.created += 1
            super().create_conversation(request)
            raise DomainError("EXECUTOR_UNAVAILABLE", "create response lost", 503)

        def send_message(self, handle, content, image_urls=()):
            self.sent += 1
            return super().send_message(handle, content, image_urls)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
        ),
    )
    runtime = CreationRetryRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        with pytest.raises(DomainError) as raised:
            conversations.bootstrap_conversation(
                db,
                workspace.id,
                work_directory_id=None,
                model_provider_id=workspace.default_model_provider_id,
                content="首条消息",
                idempotency_key="draft-create-ambiguous",
            )
        assert raised.value.code == "AGENT_BOOTSTRAP_CREATION_AMBIGUOUS"

        result = conversations.bootstrap_conversation(
            db,
            workspace.id,
            work_directory_id=None,
            model_provider_id=workspace.default_model_provider_id,
            content="首条消息",
            idempotency_key="draft-create-ambiguous",
        )
        assert result["conversation"]["lifecycle"] == "ACTIVE"
        assert runtime.created == 1
        assert runtime.sent == 1


def test_agent_workspace_bootstrap_api_requires_idempotency_key(
    client, settings, db_session_factory, monkeypatch
):
    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
            reasoning_effort=kwargs.get("reasoning_effort"),
        ),
    )
    with settings_context(settings), db_session_factory() as db:
        workspace = _ready_workspace_for_conversation(db)
        db.commit()
        workspace_id = workspace.id

    missing = client.post(
        f"/api/v1/agent-workspaces/{workspace_id}/conversations",
        json={
            "model_provider_id": "missing",
            "model_name": "test-model",
            "content": "首条消息",
        },
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "AGENT_BOOTSTRAP_IDEMPOTENCY_KEY_REQUIRED"

    created = client.post(
        f"/api/v1/agent-workspaces/{workspace_id}/conversations",
        headers={"Idempotency-Key": "bootstrap-http-001"},
        json={
            "model_provider_id": workspace.default_model_provider_id,
            "model_name": "test-model",
            "content": "首条消息",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["accepted"] is True
    assert created.json()["conversation"]["lifecycle"] == "ACTIVE"


def test_agent_workspace_message_failure_is_ambiguous_and_delete_is_tombstoned(
    settings, db_session_factory, monkeypatch
):
    class FailingRuntime(MockRuntime):
        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            raise DomainError("EXECUTOR_UNAVAILABLE", "upstream unavailable", 503)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
            reasoning_effort=kwargs.get("reasoning_effort"),
        ),
    )
    with settings_context(settings), db_session_factory() as db, runtime_context(FailingRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        try:
            conversations.message(db, workspace.id, created["id"], "hello")
        except DomainError as exc:
            assert exc.code == "AGENT_MESSAGE_DELIVERY_AMBIGUOUS"
            assert exc.status == 504
        else:
            raise AssertionError("message transport failure must not be retried")

        conversations.delete_conversation(db, workspace.id, created["id"], "delete-key")
        assert conversations.list_conversations(db, workspace.id) == []


def test_agent_workspace_uses_native_attachments_context_and_model_switch(
    settings, db_session_factory, monkeypatch
):
    class NativeWorkspaceRuntime(MockRuntime):
        sent: tuple[str, tuple[str, ...]] | None = None
        switched: RuntimeProvider | None = None

        def upload_workspace_file(self, handle, *, filename, content_type, content):
            del handle, content_type, content
            return f"/runtime/workspace/project/uploads/{'a' * 32}-{filename}"

        def send_message(self, handle, content, image_urls=()):
            self.sent = (content, image_urls)
            return super().send_message(handle, content, image_urls)

        def conversation_context(self, handle):
            del handle
            return {
                "used_tokens": None,
                "window_tokens": 128_000,
                "cumulative_tokens": 456,
                "model_name": "test-model",
                "reasoning_effort": "medium",
            }

        def switch_model(self, handle, provider):
            del handle
            self.switched = provider

    resolved_provider_ids: list[str] = []

    def resolve_provider(_db, asset, model_name=None, reasoning_effort=None):
        provider_id = asset["asset"]["executor"]["model_provider_id"]
        resolved_provider_ids.append(provider_id)
        return RuntimeProvider(
            provider_id=provider_id,
            base_url="https://models.example.test/v1",
            model=model_name or "test-model",
            api_key="x",
            reasoning_effort=reasoning_effort,
        )

    monkeypatch.setattr(conversations, "runtime_provider", resolve_provider)
    runtime = NativeWorkspaceRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        assert created["model_provider_id"] == workspace.default_model_provider_id
        assert created["model_name"] == "test-model"
        assert created["reasoning_effort"] is None
        attachment = conversations.upload_attachment(
            db,
            workspace.id,
            created["id"],
            filename="diagram.png",
            content_type="image/png",
            content=b"image-bytes",
        )
        conversations.message(
            db,
            workspace.id,
            created["id"],
            "",
            (
                {
                    "path": str(attachment["path"]),
                    "image_data_url": str(attachment["image_data_url"]),
                },
            ),
        )
        assert runtime.sent == (
            "请查看已上传到共享工作区的附件：\n"
            f"- /runtime/workspace/project/uploads/{'a' * 32}-diagram.png",
            ("data:image/png;base64,aW1hZ2UtYnl0ZXM=",),
        )
        pdf = conversations.upload_attachment(
            db,
            workspace.id,
            created["id"],
            filename="requirements.pdf",
            content_type="application/pdf",
            content=b"%PDF-1.7",
        )
        assert pdf["mime_type"] == "application/pdf"
        assert pdf["image_data_url"] is None
        conversations.message(db, workspace.id, created["id"], "", ({"path": str(pdf["path"])},))
        assert runtime.sent == (
            "请查看已上传到共享工作区的附件：\n"
            f"- /runtime/workspace/project/uploads/{'a' * 32}-requirements.pdf",
            (),
        )
        selected = conversations.switch_conversation_model(
            db,
            workspace.id,
            created["id"],
            str(workspace.default_model_provider_id),
            "test-model-2",
            "high",
        )
        assert selected == {
            "model_provider_id": workspace.default_model_provider_id,
            "model_name": "test-model-2",
            "reasoning_effort": "high",
        }
        assert runtime.switched is not None
        assert runtime.switched.model == "test-model-2"
        assert runtime.switched.reasoning_effort == "high"
        conversations.message(db, workspace.id, created["id"], "切换后发送")
        assert conversations.conversation_context(db, workspace.id, created["id"]) == {
            "used_tokens": None,
            "window_tokens": 128_000,
            "cumulative_tokens": 456,
            "model_name": "test-model",
            "reasoning_effort": "medium",
        }
        # Selecting a provider/model is persisted immediately on the individual
        # Conversation and sending only re-applies that saved selection.
        original_provider_id = workspace.default_model_provider_id
        replacement_provider = ModelProvider(
            name=f"replacement-provider-{workspace.id}",
            base_url="https://replacement.example.test/v1",
            connection_state="CONNECTED",
        )
        db.add(replacement_provider)
        db.flush()
        db.add(
            ProviderModel(
                provider_id=replacement_provider.id,
                model_name="replacement-model",
                enabled=True,
                is_default=True,
            )
        )
        workspace.default_model_provider_id = original_provider_id
        conversations.switch_conversation_model(
            db,
            workspace.id,
            created["id"],
            replacement_provider.id,
            "replacement-model",
            None,
        )
        assert runtime.switched is not None
        assert runtime.switched.model == "replacement-model"
        assert resolved_provider_ids[-1] == replacement_provider.id
        assert (
            conversations.get_conversation(db, workspace.id, created["id"])["model_provider_id"]
            == replacement_provider.id
        )
        assert conversations.get_conversation(db, workspace.id, created["id"])["model_name"] == (
            "replacement-model"
        )
        conversations.message(db, workspace.id, created["id"], "切换供应商后发送")


def test_agent_workspace_reapplies_persisted_model_after_runtime_reload_before_send(
    settings, db_session_factory, monkeypatch
):
    class RebindingRuntime(MockRuntime):
        active_provider_id = "provider-from-persisted-create"
        switched: list[tuple[str, str]] = []
        sent: list[str] = []
        fail_switch = False

        def conversation_context(self, handle):
            del handle
            return {
                "used_tokens": None,
                "window_tokens": None,
                "cumulative_tokens": None,
                "provider_id": self.active_provider_id,
                "model_name": "persisted-model",
                "reasoning_effort": None,
            }

        def switch_model(self, handle, provider):
            del handle
            if self.fail_switch:
                raise DomainError("EXECUTOR_UNAVAILABLE", "switch failed", 503)
            self.active_provider_id = provider.provider_id
            self.switched.append((provider.provider_id, provider.model))

        def send_message(self, handle, content, image_urls=()):
            self.sent.append(content)
            return super().send_message(handle, content, image_urls)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, model_name=None, **_kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=model_name or "bound-model",
            api_key="x",
        ),
    )
    runtime = RebindingRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        bound_provider_id = str(created["model_provider_id"])
        conversations.switch_conversation_model(
            db, workspace.id, created["id"], bound_provider_id, "selected-model", None
        )
        assert conversations.get_conversation(db, workspace.id, created["id"])["model_name"] == (
            "selected-model"
        )
        runtime.switched.clear()

        conversations.message(db, workspace.id, created["id"], "reload 后发送")

        assert runtime.switched == [(bound_provider_id, "selected-model")]
        assert runtime.sent == ["reload 后发送"]

        runtime.active_provider_id = "provider-from-persisted-create"
        runtime.fail_switch = True
        try:
            conversations.message(db, workspace.id, created["id"], "不得发送")
        except DomainError as exc:
            assert exc.code == "AGENT_MODEL_REBIND_FAILED"
            assert exc.status == 503
        else:
            raise AssertionError("provider rebind failure must stop the user event")
        assert runtime.sent == ["reload 后发送"]


def test_agent_workspace_forks_at_native_event_and_condenses_manually(
    settings, db_session_factory, monkeypatch
):
    class ForkRuntime(MockRuntime):
        fork_call: tuple[str | None, str] | None = None
        condensed = False

        def reload_conversation(self, handle, *, expected=None):
            del expected
            return RuntimeConversationIdentity(
                conversation_id=handle.conversation_id,
                workspace_working_dir="/runtime/workspace/project",
                persistence_dir=(
                    f"/runtime/state/conversations/{handle.conversation_id.replace('-', '')}"
                ),
                event_id="assistant-event",
                parent_id=None,
                action_id=None,
                tool_call_id=None,
            )

        def fork_conversation(self, handle, **kwargs):
            self.fork_call = (kwargs["from_event_id"], kwargs["expected_source_leaf_event_id"])
            return super().fork_conversation(handle, **kwargs)

        def condense(self, handle):
            self.condensed = True
            return super().condense(handle)

        def can_accept_input(self, handle):
            del handle
            return True

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=kwargs.get("model_name") or "test-model",
            api_key="x",
            reasoning_effort=kwargs.get("reasoning_effort"),
        ),
    )
    runtime = ForkRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        source = conversations.create_conversation(
            db, workspace.id, "源会话", workspace.default_model_provider_id, "create-key"
        )
        source_binding = db.get(AgentConversationBinding, source["id"])
        assert source_binding is not None
        conversations.switch_conversation_model(
            db,
            workspace.id,
            source["id"],
            str(workspace.default_model_provider_id),
            "fork-model",
            "high",
        )
        source_binding.streaming_callback_ready = False
        fork = conversations.fork_conversation(
            db, workspace.id, source["id"], "assistant-event", None, "fork-key"
        )
        assert fork["lifecycle"] == "ACTIVE"
        assert fork["display_title"] == "Fork · 源会话"
        assert fork["model_provider_id"] == source["model_provider_id"]
        assert fork["model_name"] == "fork-model"
        assert fork["reasoning_effort"] == "high"
        assert fork["streaming_callback_ready"] is False
        assert runtime.fork_call == ("assistant-event", "assistant-event")
        assert (
            conversations.fork_conversation(
                db, workspace.id, source["id"], "assistant-event", None, "fork-key"
            )
            == fork
        )
        assert len(conversations.list_conversations(db, workspace.id)) == 2
        condensed = conversations.condense_conversation(db, workspace.id, source["id"])
        assert condensed["accepted"] is True
        assert runtime.condensed is True


def test_agent_workspace_migrates_historical_streaming_callback_before_send(
    settings, db_session_factory, monkeypatch
):
    class StreamingMigrationRuntime(MockRuntime):
        active_provider_id: str | None = None
        calls: list[str] = []
        sent: list[str] = []
        fail_switch = False
        fail_fork = False

        def reload_conversation(self, handle, *, expected=None):
            del expected
            return RuntimeConversationIdentity(
                conversation_id=handle.conversation_id,
                workspace_working_dir="/runtime/workspace/project",
                persistence_dir=(
                    f"/runtime/state/conversations/{handle.conversation_id.replace('-', '')}"
                ),
                event_id="assistant-event",
                parent_id=None,
                action_id=None,
                tool_call_id=None,
            )

        def can_accept_input(self, handle):
            del handle
            return True

        def switch_model(self, handle, provider):
            del handle
            self.calls.append(f"switch:{provider.provider_id}")
            if self.fail_switch:
                raise DomainError("EXECUTOR_UNAVAILABLE", "switch failed", 503)
            self.active_provider_id = provider.provider_id

        def fork_conversation(self, handle, **kwargs):
            self.calls.append(f"fork:{kwargs['from_event_id']}")
            if self.fail_fork:
                raise DomainError("EXECUTOR_UNAVAILABLE", "fork failed", 503)
            return super().fork_conversation(handle, **kwargs)

        def conversation_context(self, handle):
            del handle
            return {
                "used_tokens": None,
                "window_tokens": None,
                "cumulative_tokens": None,
                "provider_id": self.active_provider_id,
                "model_name": "streaming-model",
                "reasoning_effort": "high",
            }

        def send_message(self, handle, content, image_urls=()):
            self.sent.append(content)
            return super().send_message(handle, content, image_urls)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, model_name=None, reasoning_effort=None: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model=model_name or "test-model",
            api_key="x",
            reasoning_effort=reasoning_effort,
        ),
    )
    runtime = StreamingMigrationRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        source = conversations.create_conversation(
            db, workspace.id, "历史会话", workspace.default_model_provider_id, "create-key"
        )
        assert source["streaming_callback_ready"] is True
        source_binding = db.get(AgentConversationBinding, source["id"])
        assert source_binding is not None
        source_binding.streaming_callback_ready = False
        db.flush()

        provider_id = str(workspace.default_model_provider_id)
        selected = conversations.switch_conversation_model(
            db,
            workspace.id,
            source["id"],
            provider_id,
            "streaming-model",
            "high",
        )
        assert selected == {
            "model_provider_id": provider_id,
            "model_name": "streaming-model",
            "reasoning_effort": "high",
        }
        assert runtime.calls == []

        try:
            conversations.message(db, workspace.id, source["id"], "不得写入旧会话")
        except DomainError as exc:
            assert exc.code == "AGENT_STREAMING_MIGRATION_REQUIRED"
        else:
            raise AssertionError("historical binding must be migrated before a user event")
        assert runtime.sent == []

        migrated = conversations.migrate_streaming_conversation(
            db,
            workspace.id,
            source["id"],
            provider_id,
            "streaming-model",
            "high",
            "migration-key",
        )
        assert migrated["streaming_callback_ready"] is True
        assert migrated["model_provider_id"] == provider_id
        assert migrated["display_title"] == source["display_title"]
        assert runtime.calls == [f"switch:{provider_id}", "fork:assistant-event"]

        replay = conversations.migrate_streaming_conversation(
            db,
            workspace.id,
            source["id"],
            provider_id,
            "ignored-model",
            None,
            "migration-key",
        )
        assert replay["id"] == migrated["id"]
        assert runtime.calls == [f"switch:{provider_id}", "fork:assistant-event"]

        conversations.message(db, workspace.id, migrated["id"], "只发送一次")
        assert runtime.sent == ["只发送一次"]

        failed_source = conversations.create_conversation(
            db, workspace.id, "失败源", workspace.default_model_provider_id, "failed-source"
        )
        failed_binding = db.get(AgentConversationBinding, failed_source["id"])
        assert failed_binding is not None
        failed_binding.streaming_callback_ready = False
        runtime.fail_switch = True
        try:
            conversations.migrate_streaming_conversation(
                db,
                workspace.id,
                failed_source["id"],
                provider_id,
                "streaming-model",
                None,
                "failed-migration",
            )
        except DomainError as exc:
            assert exc.code == "AGENT_STREAMING_MIGRATION_FAILED"
        else:
            raise AssertionError("failed switch must stop before native fork")
        assert runtime.sent == ["只发送一次"]
        assert runtime.calls[-1] == f"switch:{provider_id}"

        fork_failed_source = conversations.create_conversation(
            db,
            workspace.id,
            "分叉失败源",
            workspace.default_model_provider_id,
            "fork-failed-source",
        )
        fork_failed_binding = db.get(AgentConversationBinding, fork_failed_source["id"])
        assert fork_failed_binding is not None
        fork_failed_binding.streaming_callback_ready = False
        runtime.fail_switch = False
        runtime.fail_fork = True
        try:
            conversations.migrate_streaming_conversation(
                db,
                workspace.id,
                fork_failed_source["id"],
                provider_id,
                "streaming-model",
                None,
                "fork-failed-migration",
            )
        except DomainError as exc:
            assert exc.code == "EXECUTOR_UNAVAILABLE"
        else:
            raise AssertionError("failed native fork must not create a migrated user turn")
        assert runtime.sent == ["只发送一次"]
        assert runtime.calls[-2:] == [f"switch:{provider_id}", "fork:assistant-event"]


def test_agent_workspace_blocks_resend_until_native_interrupt_has_settled(
    settings, db_session_factory, monkeypatch
):
    class SerialRuntime(MockRuntime):
        ready = True
        sent: list[str] = []

        def can_accept_input(self, handle):
            del handle
            return self.ready

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            assert self.ready
            self.sent.append(content)
            self.ready = False
            return super().send_message(handle, content)

        def interrupt(self, handle):
            del handle
            self.ready = False

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = SerialRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        conversations.message(db, workspace.id, created["id"], "first")
        conversations.interrupt(db, workspace.id, created["id"])

        try:
            conversations.message(db, workspace.id, created["id"], "second")
        except DomainError as exc:
            assert exc.code == "AGENT_CONVERSATION_BUSY"
            assert exc.status == 409
        else:
            raise AssertionError("a second message cannot be sent before interrupt settles")
        assert runtime.sent == ["first"]
        assert conversations.input_readiness(db, workspace.id, created["id"]) == {"ready": False}

        runtime.ready = True
        conversations.message(db, workspace.id, created["id"], "second")
        assert runtime.sent == ["first", "second"]


def test_agent_workspace_confirmation_uses_native_batch_digest(
    settings, db_session_factory, monkeypatch
):
    class ConfirmationRuntime(MockRuntime):
        decision: tuple[str, bool, str] | None = None

        def get_pending_confirmation(self, handle):
            del handle
            return RuntimePendingConfirmation(
                pending_actions_digest="batch-digest",
                cursor="action-event",
                actions=(
                    RuntimePendingAction(
                        action_id="action-event",
                        tool_call_id="tool-call",
                        tool_name="terminal",
                        arguments={"command": "pwd"},
                        security_risk="LOW",
                        summary="查看工作目录",
                        digest="action-digest",
                    ),
                ),
            )

        def respond_to_confirmation(self, handle, expected_pending_digest, accept, reason):
            if expected_pending_digest != "batch-digest":
                raise DomainError(
                    "RUNTIME_CONFIRMATION_DRIFTED",
                    "pending confirmation changed",
                    409,
                )
            self.decision = (expected_pending_digest, accept, reason)
            return super().respond_to_confirmation(handle, expected_pending_digest, accept, reason)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = ConfirmationRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        pending = conversations.pending_confirmation(db, workspace.id, created["id"])
        assert pending == {
            "pending": True,
            "pending_actions_digest": "batch-digest",
            "cursor": "action-event",
            "actions": [
                {
                    "action_id": "action-event",
                    "tool_call_id": "tool-call",
                    "tool_name": "terminal",
                    "arguments": {"command": "pwd"},
                    "security_risk": "LOW",
                    "summary": "查看工作目录",
                    "digest": "action-digest",
                }
            ],
        }
        result = conversations.decide_confirmation(
            db,
            workspace.id,
            created["id"],
            expected_pending_digest="batch-digest",
            accept=False,
            reason=" 不需要执行 ",
        )
        assert result["accepted"] is True
        assert runtime.decision == ("batch-digest", False, "不需要执行")
        try:
            conversations.decide_confirmation(
                db,
                workspace.id,
                created["id"],
                expected_pending_digest="stale-digest",
                accept=True,
                reason="批准",
            )
        except DomainError as exc:
            assert exc.code == "RUNTIME_CONFIRMATION_DRIFTED"
            assert exc.status == 409
        else:
            raise AssertionError("a stale confirmation digest must fail closed")


def test_agent_workspace_rewrites_only_the_active_branch_last_user_message(
    settings, db_session_factory, monkeypatch
):
    class RewriteRuntime(MockRuntime):
        calls: list[tuple[str, str | None]] = []

        def read_events(self, handle):
            del handle
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        cursor="user-event",
                        event_type="MESSAGE",
                        payload={"source": "user", "content": "before", "parent_id": "root-event"},
                    ),
                )
            )

        def navigate(self, handle, event_id):
            del handle
            self.calls.append(("navigate", event_id))

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            self.calls.append(("send", content))
            return super().send_message(handle, content)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = RewriteRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        result = conversations.rewrite_message(
            db, workspace.id, created["id"], "user-event", "after"
        )
        assert result["accepted"] is True
        assert runtime.calls == [("navigate", "root-event"), ("send", "after")]
