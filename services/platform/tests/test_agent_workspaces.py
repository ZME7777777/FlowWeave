from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from flowweave.bootstrap.runtime_provider import RuntimeProviderResourceWrite
from flowweave.modules.agent_workspaces.application import conversations
from flowweave.modules.agent_workspaces.application.service import (
    ensure_default_agent_workspace,
    process_agent_workspace_runtime,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.runtime.base import RuntimeProvider
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, ManagedSandbox, TaskState
from flowweave.shared.settings import settings_context


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
    with settings_context(settings), db_session_factory() as db, runtime_context(MockRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        first = conversations.create_conversation(db, workspace.id, "第一会话", "create-key")
        replay = conversations.create_conversation(db, workspace.id, "ignored", "create-key")

        assert replay == first
        assert first["lifecycle"] == "ACTIVE"
        assert first["display_title"] == "第一会话"
        assert len(conversations.list_conversations(db, workspace.id)) == 1


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
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    with settings_context(settings), db_session_factory() as db, runtime_context(FailingRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(db, workspace.id, None, "create-key")
        try:
            conversations.message(db, workspace.id, created["id"], "hello")
        except DomainError as exc:
            assert exc.code == "AGENT_MESSAGE_DELIVERY_AMBIGUOUS"
            assert exc.status == 504
        else:
            raise AssertionError("message transport failure must not be retried")

        conversations.delete_conversation(db, workspace.id, created["id"], "delete-key")
        assert conversations.list_conversations(db, workspace.id) == []
