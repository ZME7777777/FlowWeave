from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_workspaces.application import service as agent_workspace_service
from flowweave.modules.sandboxes.infrastructure.docker import DockerSandboxProvider
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.shared.settings import settings_context


@pytest.fixture(autouse=True)
def database():
    """These Docker command-contract checks do not require PostgreSQL."""

    yield


def _persistent_resource(
    *, allocation_id: str, relative_root: str, record_id: str
) -> ManagedSandbox:
    now = datetime.now(UTC)
    return ManagedSandbox(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        kind="AGENT_RUNTIME",
        owner_type="FLOW_RUN",
        owner_id=record_id,
        backend="docker",
        backend_resource_name="fw-sbx-persistence-contract",
        image_reference="runtime:locked",
        created_at=now,
        spec_json={
            "port": 8000,
            "bound": True,
            "flow_run_id": record_id,
            "runtime_allocation_id": allocation_id,
            "runtime_allocation_relative": relative_root,
            "project_record_id": record_id,
            "environment_id": "environment-persistence-contract",
        },
        runtime_allocation_id=allocation_id,
        hard_expires_at=now + timedelta(hours=1),
        next_reconcile_at=now,
    )


def _allocation_tree(root: Path, *, allocation_id: str, record_id: str) -> None:
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    marker = root / ".flowweave-allocation"
    marker.write_text(allocation_id, encoding="ascii")
    marker.chmod(0o400)
    for relative in (
        "workspace/project",
        "workspace/nodes",
        "state/conversations",
        "state/bash-events",
        "state/persistence",
        "state/persistence/memory",
        "capabilities",
    ):
        directory = root / relative
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    project = root / "workspace/project" / record_id
    project.mkdir(mode=0o700)
    project.chmod(0o700)


def test_persistent_runtime_uses_one_openhands_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allocation_id = "11111111-1111-4111-8111-111111111111"
    record_id = "22222222-2222-4222-8222-222222222222"
    relative_root = f".flow-run-runtimes/{'a' * 32}/{record_id}"
    allocation_root = tmp_path / relative_root
    _allocation_tree(allocation_root, allocation_id=allocation_id, record_id=record_id)
    settings = Settings(
        runtime_adapter="openhands",
        workspace_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        terminal_environment_backend="docker",
        sandbox_manager_scope="persistence-contract",
        runtime_host_workspace_root=tmp_path,
        flow_run_runtime_validation_root=tmp_path,
    )
    provider = DockerSandboxProvider(settings)
    resource = _persistent_resource(
        allocation_id=allocation_id, relative_root=relative_root, record_id=record_id
    )
    monkeypatch.setattr(
        provider,
        "_ensure_environment_credential_volume",
        lambda identity: "persistence-contract-home"
        if identity == "environment-persistence-contract"
        else "unexpected",
    )

    command = provider._create_command(
        resource,
        verified_image_reference="sha256:" + "a" * 64,
        runtime_secret_key="s" * 32,
    )

    mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
    assert (
        f"type=bind,src={allocation_root / 'state/persistence'},dst=/runtime/state/persistence"
    ) in mounts
    assert (
        f"type=bind,src={allocation_root / 'state/persistence/memory'},"
        "dst=/runtime/state/persistence/memory,readonly"
    ) in mounts
    assert not any("/.openhands" in mount for mount in mounts)
    assert "OH_PERSISTENCE_DIR=/runtime/state/persistence" in command


def test_agent_workspace_runtime_shadows_all_unfrozen_memory_tiers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allocation_id = "11111111-1111-4111-8111-111111111111"
    workspace_id = "22222222-2222-4222-8222-222222222222"
    agent_user_id = "6311561c-06e4-41ad-8afe-aac35cfa83ec"
    relative_root = ".agent-workspaces/platform-default"
    allocation_root = tmp_path / relative_root
    _allocation_tree(allocation_root, allocation_id=allocation_id, record_id=workspace_id)
    disabled_project_memory = allocation_root / "state/disabled-project-memory"
    disabled_project_memory.mkdir(mode=0o700)
    disabled_project_memory.chmod(0o700)
    user_project = allocation_root / "workspace/project/users" / agent_user_id
    user_project.mkdir(mode=0o700, parents=True)
    user_project.chmod(0o700)
    settings = Settings(
        runtime_adapter="openhands",
        workspace_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        terminal_environment_backend="docker",
        sandbox_manager_scope="persistence-contract",
        runtime_host_workspace_root=tmp_path,
        flow_run_runtime_validation_root=tmp_path,
    )
    provider = DockerSandboxProvider(settings)
    now = datetime.now(UTC)
    resource = ManagedSandbox(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        kind="AGENT_RUNTIME",
        owner_type="AGENT_WORKSPACE",
        owner_id=workspace_id,
        backend="docker",
        backend_resource_name="fw-sbx-agent-memory-contract",
        image_reference="runtime:locked",
        created_at=now,
        spec_json={
            "port": 8000,
            "bound": True,
            "agent_workspace_id": workspace_id,
            "agent_user_id": agent_user_id,
            "runtime_allocation_id": allocation_id,
            "runtime_allocation_relative": relative_root,
        },
        runtime_allocation_id=allocation_id,
        hard_expires_at=now + timedelta(hours=1),
        next_reconcile_at=now,
    )
    monkeypatch.setattr(
        provider,
        "_ensure_environment_credential_volume",
        lambda identity: "agent-memory-contract-home" if identity == workspace_id else "unexpected",
    )

    command = provider._create_command(
        resource,
        verified_image_reference="sha256:" + "a" * 64,
        runtime_secret_key="s" * 32,
    )

    mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
    assert (
        f"type=bind,src={allocation_root / 'state/persistence/memory'},"
        "dst=/runtime/state/persistence/memory,readonly"
    ) in mounts
    assert (
        f"type=bind,src={disabled_project_memory},"
        f"dst=/runtime/workspace/project/users/{agent_user_id}/.openhands/memory,readonly"
    ) in mounts


def test_existing_agent_workspace_allocation_gains_memory_isolation(
    tmp_path: Path,
) -> None:
    allocation_id = "11111111-1111-4111-8111-111111111111"
    relative_root = ".agent-workspaces/platform-default"
    root = tmp_path / relative_root
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    marker = root / ".flowweave-allocation"
    marker.write_text(allocation_id, encoding="ascii")
    marker.chmod(0o400)
    for relative in agent_workspace_service._BASE_DIRECTORIES:
        directory = root / relative
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    settings = Settings(
        runtime_adapter="mock",
        workspace_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
    )
    allocation = SimpleNamespace(id=allocation_id, relative_root=relative_root)

    with settings_context(settings):
        upgraded = agent_workspace_service._upgrade_memory_isolation(allocation)

    assert upgraded == root
    for relative in agent_workspace_service._MEMORY_ISOLATION_DIRECTORIES:
        assert (root / relative).is_dir()
        assert (root / relative).stat().st_mode & 0o777 == 0o700
