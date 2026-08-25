from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from flowweave.modules.agent_workspaces.application.service import (
    ensure_default_agent_workspace,
    process_agent_workspace_runtime,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.shared.models import ManagedSandbox
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
