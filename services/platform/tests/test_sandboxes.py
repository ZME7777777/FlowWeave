from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from flowweave.modules.agent_workspaces.application.service import ensure_default_agent_workspace
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.sandboxes.application.service import (
    ReconcileReport,
    _owner_is_active,
    create_setup_sandbox,
    create_temporary_runtime,
    reconcile_managed_sandboxes,
    request_delete_durable,
    touch_runtime,
)
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_rollback_actions,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure import docker_control
from flowweave.shared.infrastructure.docker_control import (
    DockerOwnershipError,
    remove_owned_container,
    remove_owned_network,
    remove_owned_volume,
    run_docker_with_storage_quota_fallback,
)
from flowweave.shared.models import ManagedSandbox
from flowweave.shared.settings import settings_context


def test_docker_storage_quota_fallback_retries_only_without_fixed_quota() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                125,
                stdout="",
                stderr=(
                    "docker: Error response from daemon: --storage-opt is supported only "
                    "for overlay over xfs with 'pquota' mount option"
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="container-id", stderr="")

    command = [
        "docker",
        "run",
        "--storage-opt",
        "size=4g",
        "--read-only",
        "--memory",
        "128m",
        "locked-image",
    ]
    completed = run_docker_with_storage_quota_fallback(
        command,
        timeout=30,
        runner=fake_run,
    )

    assert completed.returncode == 0
    assert calls == [
        command,
        [
            "docker",
            "run",
            "--read-only",
            "--memory",
            "128m",
            "locked-image",
        ],
    ]


def test_docker_storage_quota_fallback_keeps_unrelated_failures_closed() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            125,
            stdout="",
            stderr="docker: Error response from daemon: pull access denied",
        )

    command = ["docker", "run", "--storage-opt", "size=4g", "locked-image"]
    completed = run_docker_with_storage_quota_fallback(
        command,
        timeout=30,
        runner=fake_run,
    )

    assert completed.returncode == 125
    assert calls == [command]


def _resource(*, expired: bool = False, desired_state: str = "RUNNING") -> ManagedSandbox:
    now = datetime.now(UTC)
    return ManagedSandbox(
        kind="ENVIRONMENT_SETUP",
        # Generic reconciler tests exercise observation/claim behavior rather
        # than business-owner lifecycle. Unknown owners are allowed only during
        # the bounded binding grace period; dedicated tests below use every
        # supported owner type and verify fail-closed cleanup.
        owner_type="TEST_OWNER",
        owner_id="setup-owner",
        backend="docker",
        backend_resource_name="fw-sbx-test",
        desired_state=desired_state,
        observed_state="RUNNING",
        image_reference="runtime:locked",
        spec_json={"environment_id": "environment-1"},
        hard_expires_at=now - timedelta(seconds=1) if expired else now + timedelta(hours=1),
        next_reconcile_at=now - timedelta(seconds=1),
    )


def _observation(resource: ManagedSandbox, *, resource_id: str | None = None) -> DockerObservation:
    actual_id = resource_id if resource_id is not None else resource.id
    return DockerObservation(
        resource_id=actual_id,
        resource_name=resource.backend_resource_name,
        resource_identifier="docker-container-id",
        state="RUNNING",
        labels={
            "flowweave.managed": "true",
            "flowweave.manager-scope": "test-scope",
            "flowweave.resource-id": actual_id,
            "flowweave.kind": resource.kind.lower().replace("_", "-"),
            "flowweave.owner-type": resource.owner_type,
            "flowweave.owner-id": resource.owner_id,
            "flowweave.image-reference": resource.image_reference,
            "flowweave.spec-hash": DockerSandboxProvider._spec_hash(resource),
        },
    )


def _docker_settings(settings, **updates):
    return settings.model_copy(
        update={
            "terminal_environment_backend": "docker",
            "sandbox_manager_scope": "test-scope",
            **updates,
        }
    )


def _runtime_resource(workspace_relative: str = "nodes/node-1") -> ManagedSandbox:
    now = datetime.now(UTC)
    return ManagedSandbox(
        id="12345678-1234-4234-9234-123456789abc",
        kind="AGENT_RUNTIME",
        owner_type="CAPABILITY_VALIDATION",
        owner_id="validation-1",
        backend="docker",
        backend_resource_name="fw-sbx-runtime",
        image_reference="runtime:locked",
        created_at=now,
        spec_json={
            "port": 8000,
            "bound": False,
            "workspace_relative": workspace_relative,
            "environment_id": "environment-1",
            "environment_version_id": "environment-version-1",
            "environment_version_no": 1,
        },
        hard_expires_at=now + timedelta(hours=1),
        next_reconcile_at=now,
    )


def test_provider_refuses_to_delete_a_name_owned_by_another_resource(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _resource(desired_state="DELETED")
    resource.id = "expected-resource"

    def ownership_conflict(*_args, **_kwargs):
        from flowweave.shared.infrastructure.docker_control import DockerOwnershipError

        raise DockerOwnershipError("different owner")

    monkeypatch.setattr(
        "flowweave.modules.sandboxes.infrastructure.docker.remove_owned_container",
        ownership_conflict,
    )

    with pytest.raises(DomainError) as caught:
        provider.delete(resource)

    assert caught.value.code == "SANDBOX_RESOURCE_CONFLICT"


def test_provider_treats_docker_no_such_object_as_an_absent_container(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))

    def missing(*_args, **_kwargs):
        raise DomainError(
            "SANDBOX_DOCKER_FAILED",
            "The Docker sandbox operation failed",
            502,
            {"detail": "Error: No such object: fw-sbx-missing"},
        )

    monkeypatch.setattr(provider, "_run", missing)

    assert provider.inspect("fw-sbx-missing") is None


def test_owned_container_delete_is_idempotent_for_docker_no_such_object(monkeypatch):
    commands: list[list[str]] = []

    def missing(command: list[str], *, timeout: int):
        del timeout
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error: No such object: fw-sbx-missing",
        )

    monkeypatch.setattr(docker_control, "_run", missing)

    assert (
        remove_owned_container(
            "docker",
            "fw-sbx-missing",
            "resource-missing",
            expected_manager_scope="test-scope",
        )
        is False
    )
    assert [command[1] for command in commands] == ["inspect"]


def test_provider_treats_named_network_not_found_as_absent(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))

    def missing(*_args, **_kwargs):
        raise DomainError(
            "SANDBOX_DOCKER_FAILED",
            "The Docker sandbox operation failed",
            502,
            {"detail": ("Error response from daemon: network fw-net-missing not found")},
        )

    monkeypatch.setattr(provider, "_run", missing)

    resource = _runtime_resource()
    assert provider._inspect_runtime_network(resource) is None


def test_owned_network_delete_is_idempotent_for_named_network_not_found(monkeypatch):
    commands: list[list[str]] = []

    def missing(command: list[str], *, timeout: int):
        del timeout
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error response from daemon: network fw-net-missing not found",
        )

    monkeypatch.setattr(docker_control, "_run", missing)

    assert (
        remove_owned_network(
            "docker",
            "fw-net-missing",
            "resource-missing",
            expected_manager_scope="test-scope",
        )
        is False
    )
    assert [command[1:3] for command in commands] == [["network", "inspect"]]


def test_environment_credential_volume_names_are_stable_and_isolated(settings):
    provider = DockerSandboxProvider(_docker_settings(settings))

    first = provider.environment_credential_volume_name("environment-1")
    repeated = provider.environment_credential_volume_name("environment-1")
    second = provider.environment_credential_volume_name("environment-2")

    assert first == repeated
    assert first != second
    assert first.startswith("fw-env-auth-")
    assert len(first) == len("fw-env-auth-") + 32


def test_owned_environment_credential_volume_rejects_wrong_labels(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        del timeout
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                [
                    {
                        "Name": "fw-env-auth-owned",
                        "Labels": {
                            "flowweave.managed": "true",
                            "flowweave.resource-type": "environment-credential-volume",
                            "flowweave.environment-id": "another-environment",
                            "flowweave.manager-scope": "test-scope",
                        },
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(docker_control, "_run", fake_run)

    with pytest.raises(DockerOwnershipError):
        remove_owned_volume(
            "docker",
            "fw-env-auth-owned",
            expected_environment_id="environment-1",
            expected_manager_scope="test-scope",
        )

    assert [command[1:3] for command in commands] == [["volume", "inspect"]]


def test_runtime_conflict_is_rejected_before_network_creation(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    conflict = _observation(resource, resource_id="another-resource")
    monkeypatch.setattr(provider, "inspect", lambda _name: conflict)
    network_touched: list[str] = []
    monkeypatch.setattr(
        provider,
        "_ensure_runtime_network",
        lambda item: network_touched.append(item.id) or "unused",
    )

    with pytest.raises(DomainError) as caught:
        provider.ensure_running(resource)

    assert caught.value.code == "SANDBOX_RESOURCE_CONFLICT"
    assert network_touched == []


def test_untrusted_runtime_image_is_rejected_before_network_creation(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    network_touched: list[str] = []
    monkeypatch.setattr(provider, "inspect", lambda _name: None)
    monkeypatch.setattr(
        provider,
        "_ensure_runtime_network",
        lambda item: network_touched.append(item.id) or "unused",
    )

    with pytest.raises(DomainError) as caught:
        provider.ensure_running(resource)

    assert caught.value.code == "SANDBOX_IMAGE_UNTRUSTED"
    assert network_touched == []


def test_existing_container_spec_drift_is_rejected_before_network_creation(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    observation = _observation(resource)
    observation.labels["flowweave.spec-hash"] = "0" * 64
    network_touched: list[str] = []
    monkeypatch.setattr(provider, "inspect", lambda _name: observation)
    monkeypatch.setattr(
        provider,
        "_ensure_runtime_network",
        lambda item: network_touched.append(item.id) or "unused",
    )

    with pytest.raises(DomainError) as caught:
        provider.ensure_running(resource)

    assert caught.value.code == "SANDBOX_RESOURCE_CONFLICT"
    assert "flowweave.spec-hash" in caught.value.details["mismatches"]
    assert network_touched == []


def test_existing_owned_runtime_does_not_require_pruned_historical_image(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    observation = _observation(resource)
    network_touched: list[str] = []

    monkeypatch.setattr(provider, "inspect", lambda _name: observation)
    monkeypatch.setattr(
        provider,
        "_verify_image_trust",
        lambda _item: (_ for _ in ()).throw(AssertionError("must not inspect a pruned image")),
    )
    monkeypatch.setattr(
        provider,
        "_ensure_runtime_network",
        lambda item: network_touched.append(item.id) or "unused",
    )
    monkeypatch.setattr(provider, "_isolate_runtime_container", lambda *_args: None)
    monkeypatch.setattr(provider, "_wait_for_agent_server", lambda _name: None)

    assert provider.ensure_running(resource) == observation
    assert network_touched == [resource.id]


def test_setup_ledger_survives_outer_rollback_as_delete_intent(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    monkeypatch.setattr(DockerSandboxProvider, "require_enabled", lambda self: None)
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, item, **_kwargs: _observation(item),
    )

    with settings_context(configured), db_session_factory() as db:
        mark_uow_owned(db)
        resource = create_setup_sandbox(
            db,
            owner_id="setup-that-rolls-back",
            environment_id="environment-1",
            image="runtime:locked",
            base_image_reference="python@sha256:" + "1" * 64,
            base_image_digest="sha256:" + "1" * 64,
            base_version_id=None,
            base_version_no=None,
            hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        resource_id = resource.id
        db.rollback()
        run_rollback_actions(db)

    with db_session_factory() as db:
        persisted = db.get(ManagedSandbox, resource_id)
        assert persisted is not None
        assert persisted.desired_state == "DELETED"
        assert persisted.next_reconcile_at <= datetime.now(UTC)


def test_owned_delete_requires_matching_resource_and_manager_scope(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "Id": "container-id",
                    "Config": {
                        "Labels": {
                            "flowweave.managed": "true",
                            "flowweave.manager-scope": "other-scope",
                            "flowweave.resource-id": "resource-1",
                        }
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(docker_control.subprocess, "run", fake_run)
    with pytest.raises(DockerOwnershipError):
        remove_owned_container(
            "docker",
            "container-name",
            "resource-1",
            expected_manager_scope="test-scope",
        )

    assert [command[1] for command in commands] == ["inspect"]


def test_runtime_bind_mount_uses_explicit_host_root_without_source_container(settings, tmp_path):
    (tmp_path / "nodes/node-1").mkdir(parents=True)
    (tmp_path / ".managed-assets/nodes/node-1").mkdir(parents=True)
    provider = DockerSandboxProvider(
        _docker_settings(
            settings,
            runtime_host_workspace_root=tmp_path,
            flow_run_runtime_validation_root=tmp_path,
        )
    )

    mount = provider._runtime_workspace_mount(_runtime_resource())

    assert mount == [
        "--mount",
        f"type=bind,src={tmp_path}/nodes/node-1,dst=/workspaces/nodes/node-1",
        "--mount",
        (
            f"type=bind,src={tmp_path}/.managed-assets/nodes/node-1,"
            "dst=/runtime/capabilities/nodes/node-1,readonly"
        ),
    ]


def test_runtime_command_is_non_root_read_only_and_has_only_bounded_writable_paths(
    settings, monkeypatch
):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    monkeypatch.setattr(
        provider,
        "_runtime_workspace_mount",
        lambda _resource: [
            "--mount",
            "type=bind,src=/srv/workspaces/nodes/node-1,dst=/workspaces/nodes/node-1",
            "--mount",
            (
                "type=bind,src=/srv/workspaces/.managed-assets/nodes/node-1,"
                "dst=/runtime/capabilities/nodes/node-1,readonly"
            ),
        ],
    )
    credential_volume = provider.environment_credential_volume_name("environment-1")
    monkeypatch.setattr(
        provider,
        "_ensure_environment_credential_volume",
        lambda environment_id: credential_volume
        if environment_id == "environment-1"
        else "unexpected",
    )

    command = provider._create_command(resource, verified_image_reference="sha256:" + "a" * 64)

    assert command[command.index("--user") + 1] == "10001:10001"
    assert "--read-only" in command
    assert "--privileged" not in command
    mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
    assert mounts == [
        (f"type=volume,src={credential_volume},dst=/home/flowweave"),
        "type=bind,src=/srv/workspaces/nodes/node-1,dst=/workspaces/nodes/node-1",
        (
            "type=bind,src=/srv/workspaces/.managed-assets/nodes/node-1,"
            "dst=/runtime/capabilities/nodes/node-1,readonly"
        ),
    ]
    assert all("/var/run/docker.sock" not in mount for mount in mounts)
    tmpfs = [command[index + 1] for index, item in enumerate(command) if item == "--tmpfs"]
    assert tmpfs == [
        "/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=1777",
        "/runtime/ephemeral-state:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
    ]
    assert "HOME=/home/flowweave" in command
    assert "OPENHANDS_SUPPRESS_BANNER=1" in command


def test_setup_and_runtime_share_the_same_complete_environment_home(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    volume = provider.environment_credential_volume_name("environment-1")
    monkeypatch.setattr(provider, "_ensure_environment_credential_volume", lambda _id: volume)
    monkeypatch.setattr(
        provider,
        "_runtime_workspace_mount",
        lambda _resource: ["--mount", "type=bind,src=/safe,dst=/workspaces/node"],
    )

    setup = _resource()
    setup.id = "12345678-1234-4234-9234-123456789abd"
    setup.created_at = datetime.now(UTC)
    runtime = _runtime_resource()
    image = "sha256:" + "a" * 64

    setup_command = provider._create_command(setup, verified_image_reference=image)
    runtime_command = provider._create_command(runtime, verified_image_reference=image)
    setup_mounts = [
        setup_command[index + 1] for index, item in enumerate(setup_command) if item == "--mount"
    ]
    runtime_mounts = [
        runtime_command[index + 1]
        for index, item in enumerate(runtime_command)
        if item == "--mount"
    ]

    assert f"type=volume,src={volume},dst=/root" in setup_mounts
    assert f"type=volume,src={volume},dst=/home/flowweave" in runtime_mounts
    assert "HOME=/root" in setup_command
    assert "HOME=/home/flowweave" in runtime_command
    assert runtime_command[runtime_command.index("--user") + 1] == "10001:10001"


def test_environment_home_preparation_is_networkless_and_migrates_legacy_lark_layout(
    settings, monkeypatch
):
    provider = DockerSandboxProvider(_docker_settings(settings))
    calls: list[tuple[list[str], int]] = []
    monkeypatch.setattr(
        provider,
        "_run",
        lambda command, *, timeout=60: calls.append((command, timeout)) or "",
    )

    image = "sha256:" + "b" * 64
    provider._prepare_environment_home("environment-home", verified_image_reference=image)

    assert len(calls) == 1
    command, timeout = calls[0]
    assert timeout == 60
    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "0:0"
    assert "--privileged" not in command
    assert command.count("--cap-add") == 3
    assert "type=volume,src=environment-home,dst=/flowweave-home" in command
    script = command[-1]
    assert 'mv "$target/$entry" "$target/.lark-cli/$entry"' in script
    assert 'cp -a -n /root/. "$target/"' in script
    assert 'mkdir -p "$target/.openhands"' in script
    assert 'chmod 0700 "$target/.openhands"' in script
    assert 'chown -R 10001:10001 "$target"' in script


@pytest.mark.parametrize(
    ("network_mode", "expected_internal"),
    (("isolated", True), ("egress", False)),
)
def test_runtime_network_contract_requires_declared_mode(
    settings, monkeypatch, network_mode, expected_internal
):
    provider = DockerSandboxProvider(
        _docker_settings(settings, sandbox_runtime_network_mode=network_mode)
    )
    resource = _runtime_resource()
    network_name = provider._runtime_network_name(resource.id)

    def inspection(
        *,
        driver: str = "bridge",
        internal: bool = expected_internal,
        name: str = network_name,
        labelled_mode: str = network_mode,
    ):
        return json.dumps(
            [
                {
                    "Name": name,
                    "Driver": driver,
                    "Internal": internal,
                    "Labels": {
                        "flowweave.managed": "true",
                        "flowweave.resource-type": "network",
                        "flowweave.resource-id": resource.id,
                        "flowweave.manager-scope": "test-scope",
                        "flowweave.network-purpose": "agent-runtime",
                        "flowweave.network-mode": labelled_mode,
                    },
                }
            ]
        )

    monkeypatch.setattr(provider, "_run", lambda *_args, **_kwargs: inspection())
    assert provider._inspect_runtime_network(resource) is not None

    for invalid in (
        inspection(driver="host"),
        inspection(internal=not expected_internal),
        inspection(name="shared-runtime-network"),
        inspection(labelled_mode="egress" if network_mode == "isolated" else "isolated"),
    ):
        monkeypatch.setattr(provider, "_run", lambda *_args, value=invalid, **_kwargs: value)
        with pytest.raises(DomainError) as caught:
            provider._inspect_runtime_network(resource)
        assert caught.value.code == "SANDBOX_RESOURCE_CONFLICT"


@pytest.mark.parametrize(
    ("network_mode", "expects_internal_flag"),
    (("isolated", True), ("egress", False)),
)
def test_runtime_network_creation_applies_declared_mode(
    settings, monkeypatch, network_mode, expects_internal_flag
):
    provider = DockerSandboxProvider(
        _docker_settings(settings, sandbox_runtime_network_mode=network_mode)
    )
    resource = _runtime_resource()
    commands: list[list[str]] = []
    inspections = iter((None, {"Name": provider._runtime_network_name(resource.id)}))
    monkeypatch.setattr(provider, "_inspect_runtime_network", lambda _resource: next(inspections))
    monkeypatch.setattr(provider, "_trusted_runtime_clients", lambda: ["worker-container-id"])
    monkeypatch.setattr(provider, "_connect_network", lambda *_args: None)
    monkeypatch.setattr(provider, "_run", lambda command, **_kwargs: commands.append(command) or "")

    provider._ensure_runtime_network(resource)

    assert len(commands) == 1
    command = commands[0]
    assert ("--internal" in command) is expects_internal_flag
    assert "flowweave.network-purpose=agent-runtime" in command
    assert f"flowweave.network-mode={network_mode}" in command


def test_setup_uses_an_owned_per_session_egress_network(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _resource()
    resource.id = "12345678-1234-4234-9234-123456789abc"
    resource.created_at = datetime.now(UTC)
    commands: list[list[str]] = []
    inspections = iter((None, {"Name": provider._runtime_network_name(resource.id)}))
    monkeypatch.setattr(provider, "_inspect_runtime_network", lambda _resource: next(inspections))
    monkeypatch.setattr(provider, "_run", lambda command, **_kwargs: commands.append(command) or "")

    network_name = provider._ensure_runtime_network(resource)

    assert network_name == provider._runtime_network_name(resource.id)
    assert len(commands) == 1
    assert "--internal" not in commands[0]
    assert "flowweave.network-purpose=environment-setup" in commands[0]
    assert "flowweave.network-mode=egress" in commands[0]
    assert all(command[1:3] != ["network", "connect"] for command in commands)


def test_managed_sandbox_commands_bound_docker_logs(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    monkeypatch.setattr(
        provider,
        "_runtime_workspace_mount",
        lambda _resource: ["--mount", "type=bind,src=/safe,dst=/workspaces/node"],
    )
    monkeypatch.setattr(provider, "_ensure_environment_credential_volume", lambda _id: "auth")

    command = provider._create_command(resource, verified_image_reference="sha256:" + "a" * 64)

    assert command[command.index("--log-driver") + 1] == "local"
    log_options = [command[index + 1] for index, item in enumerate(command) if item == "--log-opt"]
    assert log_options == ["max-size=8m", "max-file=2"]
    assert command[command.index("--storage-opt") + 1] == "size=4g"


def test_setup_base_digest_is_verified_for_launch(settings, monkeypatch):
    configured = _docker_settings(settings)
    provider = DockerSandboxProvider(configured)
    resource = _resource()
    resource.id = "12345678-1234-4234-9234-123456789abc"
    resource.created_at = datetime.now(UTC)
    immutable_id = "sha256:" + "b" * 64
    base_reference = "flowweave/setup@" + immutable_id
    resource.image_reference = immutable_id
    resource.spec_json = {
        "environment_id": "environment-1",
        "base_version_id": None,
        "base_version_no": None,
        "base_image_reference": base_reference,
        "base_image_digest": immutable_id,
    }
    inspected: list[list[str]] = []

    def inspect_image(command: list[str], **_kwargs):
        inspected.append(command)
        return json.dumps({"Id": immutable_id, "Config": {"Labels": {}}})

    monkeypatch.setattr(provider, "_run", inspect_image)
    monkeypatch.setattr(
        provider, "_ensure_environment_credential_volume", lambda _id: "auth-volume"
    )

    verified = provider._verify_image_trust(resource)
    command = provider._create_command(resource, verified_image_reference=verified)

    assert inspected == [
        [
            "docker",
            "image",
            "inspect",
            immutable_id,
            "--format",
            "{{json .}}",
        ]
    ]
    assert command[-4] == immutable_id
    assert f"flowweave.image-reference={resource.image_reference}" in command


def test_setup_base_image_inspection_fails_closed_without_immutable_id(settings, monkeypatch):
    configured = _docker_settings(settings)
    provider = DockerSandboxProvider(configured)
    resource = _resource()
    immutable_id = "sha256:" + "b" * 64
    resource.image_reference = immutable_id
    resource.spec_json = {
        "environment_id": "environment-1",
        "base_version_id": None,
        "base_version_no": None,
        "base_image_reference": "flowweave/setup@" + immutable_id,
        "base_image_digest": immutable_id,
    }
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *_args, **_kwargs: json.dumps(
            {"Id": "flowweave/setup:mutable", "Config": {"Labels": {}}}
        ),
    )

    with pytest.raises(DomainError) as caught:
        provider._verify_image_trust(resource)

    assert caught.value.code == "SANDBOX_DOCKER_PROTOCOL_ERROR"


def test_create_command_rejects_mutable_launch_reference(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    resource = _runtime_resource()
    monkeypatch.setattr(
        provider, "_ensure_environment_credential_volume", lambda _id: "auth-volume"
    )

    with pytest.raises(DomainError) as caught:
        provider._create_command(resource, verified_image_reference="flowweave/runtime:latest")

    assert caught.value.code == "SANDBOX_IMAGE_UNTRUSTED"


def test_runtime_network_mode_rejects_unknown_value(settings):
    with pytest.raises(ValueError, match="SANDBOX_RUNTIME_NETWORK_MODE"):
        settings.model_copy(
            update={"sandbox_runtime_network_mode": "unrestricted"}
        ).validate_production_secrets()


def test_runtime_clients_require_api_or_worker_role_and_current_scope(settings, monkeypatch):
    provider = DockerSandboxProvider(_docker_settings(settings))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        return (
            "api-container-id\n"
            if "label=flowweave.runtime-client-role=api" in command
            else "worker-container-id\n"
        )

    monkeypatch.setattr(provider, "_run", fake_run)

    assert provider._trusted_runtime_clients() == ["api-container-id", "worker-container-id"]
    assert commands == [
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            "label=flowweave.runtime-client=true",
            "--filter",
            "label=flowweave.runtime-client-role=api",
            "--filter",
            "label=flowweave.manager-scope=test-scope",
        ],
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            "label=flowweave.runtime-client=true",
            "--filter",
            "label=flowweave.runtime-client-role=worker",
            "--filter",
            "label=flowweave.manager-scope=test-scope",
        ],
    ]


@pytest.mark.parametrize(
    "managed_root",
    [
        "/workspaces/managed",
        "/",
        "relative/capabilities",
        "/runtime/capabilities,invalid",
    ],
)
def test_runtime_managed_asset_mount_root_must_be_disjoint(settings, monkeypatch, managed_root):
    provider = DockerSandboxProvider(
        _docker_settings(settings, openhands_managed_assets_root=managed_root)
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(provider, "_run", lambda command, **_kwargs: calls.append(command))

    with pytest.raises(DomainError) as caught:
        provider._runtime_workspace_mount(_runtime_resource())

    assert caught.value.code == "SANDBOX_WORKSPACE_INVALID"
    assert calls == []


@pytest.mark.parametrize("workspace_relative", ["../other-node", "/absolute/node"])
def test_runtime_workspace_mount_fails_closed(settings, workspace_relative):
    provider = DockerSandboxProvider(_docker_settings(settings))

    with pytest.raises(DomainError) as caught:
        provider._runtime_workspace_mount(_runtime_resource(workspace_relative))

    assert caught.value.code in {
        "SANDBOX_WORKSPACE_INVALID",
        "SANDBOX_WORKSPACE_SOURCE_INVALID",
    }


def test_reconciler_recreates_a_missing_expected_resource(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    with db_session_factory() as db:
        resource = _resource()
        db.add(resource)
        db.commit()
        resource_id = resource.id

    created: list[str] = []
    monkeypatch.setattr(DockerSandboxProvider, "inspect", lambda self, _name: None)

    def ensure_running(self, item, **_kwargs):
        del self
        created.append(item.id)
        return _observation(item)

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        persisted = db.get(ManagedSandbox, resource_id)
        assert persisted is not None
        assert persisted.observed_state == "RUNNING"
        assert persisted.backend_resource_id == "docker-container-id"
        assert persisted.last_error_code is None
    assert report.inspected == 1
    assert created == [resource_id]


def test_reconciler_does_not_recreate_a_missing_bound_runtime(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="FLOW_RUN",
            owner_id="11111111-1111-4111-8111-111111111111",
            backend="docker",
            backend_resource_name="fw-sbx-bound-runtime",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"port": 8000, "bound": True},
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    recreated: list[str] = []
    monkeypatch.setattr(DockerSandboxProvider, "inspect", lambda self, _name: None)
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, item, **_kwargs: recreated.append(item.id) or _observation(item),
    )
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        persisted = db.get(ManagedSandbox, resource_id)
        assert persisted is not None
        assert persisted.observed_state == "ERROR"
        assert persisted.last_error_code == "SANDBOX_RUNTIME_LOST"
    assert report.errors == 1
    assert recreated == []


def test_reconciler_keeps_agent_runtime_when_docker_control_is_temporarily_unavailable(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert runtime is not None
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="AGENT_WORKSPACE",
            owner_id=workspace.id,
            backend="docker",
            backend_resource_name="fw-sbx-agent-runtime",
            observed_state="READY",
            image_reference=runtime.runtime_image_digest,
            agent_workspace_allocation_id=runtime.workspace_allocation_id,
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.flush()
        runtime.active_generation = resource.generation
        runtime.status = "ACTIVE"
        db.add(
            AgentWorkspaceRuntimeGeneration(
                runtime_session_id=runtime.id,
                generation=resource.generation,
                managed_runtime_id=resource.id,
                runtime_image_digest=runtime.runtime_image_digest,
                state="READY",
                fence_token="12345678-1234-4234-9234-123456789abc",
            )
        )
        db.commit()
        resource_id = resource.id
        workspace_id = workspace.id

    def unavailable(self, _name):
        del self
        raise DomainError("SANDBOX_BACKEND_UNAVAILABLE", "Docker temporarily unavailable", 503)

    monkeypatch.setattr(DockerSandboxProvider, "inspect", unavailable)
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        resource = db.get(ManagedSandbox, resource_id)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace_id)
        )
        assert resource is not None
        assert runtime is not None
        assert resource.desired_state == "RUNNING"
        assert resource.observed_state == "ERROR"
        assert resource.last_error_code == "SANDBOX_BACKEND_UNAVAILABLE"
        assert runtime.status == "ACTIVE"
        assert runtime.failure_code is None
    assert report.errors == 1


def test_reconciler_deletes_auxiliary_resources_when_container_is_already_missing(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="TEST_OWNER",
            owner_id="attempt-network-only",
            backend="docker",
            backend_resource_name="fw-sbx-network-only",
            desired_state="DELETED",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"port": 8000, "bound": False},
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    deleted: list[str] = []
    monkeypatch.setattr(DockerSandboxProvider, "inspect", lambda self, _name: None)
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda self, item: deleted.append(item.id))
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    with db_session_factory() as db:
        assert db.get(ManagedSandbox, resource_id) is None
    assert report.deleted == 1
    assert deleted == [resource_id]


def test_runtime_reallocation_increments_generation(settings, db_session_factory, monkeypatch):
    configured = _docker_settings(settings)

    def ensure_running(self, item, **_kwargs):
        del self
        return _observation(item)

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    with settings_context(configured), db_session_factory() as db:
        first = create_temporary_runtime(
            db,
            owner_type="CAPABILITY_VALIDATION",
            owner_id="attempt-generation",
            image="runtime:locked",
            environment_id="environment-1",
            environment_version_id="environment-version-1",
            environment_version_no=1,
            workspace_relative="nodes/node-1",
        )
        request_delete_durable(db, first.id)
        second = create_temporary_runtime(
            db,
            owner_type="CAPABILITY_VALIDATION",
            owner_id="attempt-generation",
            image="runtime:locked",
            environment_id="environment-1",
            environment_version_id="environment-version-1",
            environment_version_no=1,
            workspace_relative="nodes/node-1",
        )

    assert second.id != first.id
    with db_session_factory() as db:
        resources = list(
            db.scalars(
                select(ManagedSandbox)
                .where(ManagedSandbox.owner_id == "attempt-generation")
                .order_by(ManagedSandbox.generation)
            )
        )
    assert [item.generation for item in resources] == [1, 2]


@pytest.mark.asyncio
async def test_durable_runtime_control_writes_survive_async_caller_rollback(
    settings, container, db_session_factory
):
    configured = _docker_settings(
        settings,
        sandbox_runtime_idle_ttl_seconds=600,
        sandbox_runtime_hard_ttl_seconds=3600,
    )
    now = datetime.now(UTC)
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="CAPABILITY_VALIDATION",
            owner_id="attempt-durable-control",
            backend="docker",
            backend_resource_name="fw-sbx-durable-control",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"port": 8000},
            last_activity_at=now - timedelta(minutes=30),
            idle_expires_at=now - timedelta(minutes=1),
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now,
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    with settings_context(configured):
        async with container.database.session() as outer:
            await outer.run_sync(lambda db: touch_runtime(db, resource_id))
            await outer.rollback()

            with db_session_factory() as db:
                touched = db.get(ManagedSandbox, resource_id)
                assert touched is not None
                assert touched.idle_expires_at is not None
                assert touched.idle_expires_at > touched.last_activity_at
            first_activity = touched.last_activity_at
            touched.last_activity_at = now - timedelta(minutes=5)
            db.commit()

        async with container.database.session() as outer:
            await outer.run_sync(lambda db: touch_runtime(db, resource_id))
            await outer.rollback()

            with db_session_factory() as db:
                touched = db.get(ManagedSandbox, resource_id)
                assert touched is not None
                assert touched.idle_expires_at is not None
                assert touched.idle_expires_at > touched.last_activity_at
            assert touched.last_activity_at > first_activity

        async with container.database.session() as outer:
            await outer.run_sync(lambda db: request_delete_durable(db, resource_id))
            await outer.rollback()

    with db_session_factory() as db:
        deleted = db.get(ManagedSandbox, resource_id)
        assert deleted is not None
        assert deleted.desired_state == "DELETED"
        assert deleted.next_reconcile_at <= datetime.now(UTC)


def test_reconciler_deletes_idle_runtime_before_hard_limit(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="ATTEMPT",
            owner_id="attempt-idle-expired",
            backend="docker",
            backend_resource_name="fw-sbx-idle-expired",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"port": 8000, "bound": False},
            idle_expires_at=now - timedelta(seconds=1),
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    deleted: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider, "inspect", lambda self, _name: _observation(resource)
    )
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda self, item: deleted.append(item.id))
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        assert db.get(ManagedSandbox, resource_id) is None
    assert report.expired == 1
    assert deleted == [resource_id]


def test_reconciler_drains_legacy_attempt_runtime_owner(settings, db_session_factory, monkeypatch):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="ATTEMPT",
            owner_id="attempt-bound-active-expired",
            backend="docker",
            backend_resource_name="fw-sbx-bound-active-expired",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"port": 8000, "bound": True},
            idle_expires_at=None,
            hard_expires_at=now - timedelta(seconds=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    deleted: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider, "inspect", lambda self, _name: _observation(resource)
    )
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda self, item: deleted.append(item.id))
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    with db_session_factory() as db:
        assert db.get(ManagedSandbox, resource_id) is None
    assert report.expired == 1
    assert report.deleted == 1
    assert deleted == [resource_id]


def test_reconciler_dispatches_mcp_oauth_authorization_owner(db_session_factory, monkeypatch):
    now = datetime.now(UTC)
    resource = ManagedSandbox(
        kind="AGENT_RUNTIME",
        owner_type="MCP_OAUTH_AUTHORIZATION",
        owner_id="authorization-active",
        backend="docker",
        backend_resource_name="fw-sbx-oauth-active",
        observed_state="RUNNING",
        image_reference="runtime:locked",
        spec_json={"port": 8000, "bound": False},
        created_at=now - timedelta(minutes=10),
        idle_expires_at=now + timedelta(minutes=5),
        hard_expires_at=now + timedelta(hours=1),
        next_reconcile_at=now,
    )
    observed: list[str] = []
    monkeypatch.setattr(
        "flowweave.modules.catalog.public.mcp_oauth_authorization_owner_is_active",
        lambda _db, owner_id: observed.append(owner_id) or True,
    )
    with db_session_factory() as db:
        assert _owner_is_active(db, resource, now=now, binding_grace_seconds=1) is True
    assert observed == ["authorization-active"]


@pytest.mark.parametrize(
    "owner_type",
    ["SETUP_SESSION", "ATTEMPT", "CONVERSATION"],
)
def test_reconciler_deletes_bound_sandbox_when_owner_is_terminal(
    settings, db_session_factory, monkeypatch, owner_type
):
    configured = _docker_settings(settings)
    now = datetime.now(UTC)
    # An absent owner covers deletion/cascade races for every supported type.
    with db_session_factory() as db:
        resource = ManagedSandbox(
            kind="ENVIRONMENT_SETUP" if owner_type == "SETUP_SESSION" else "AGENT_RUNTIME",
            owner_type=owner_type,
            owner_id=f"absent-{owner_type.lower()}",
            backend="docker",
            backend_resource_name=f"fw-sbx-absent-{owner_type.lower()}",
            observed_state="RUNNING",
            image_reference="runtime:locked",
            spec_json={"bound": True},
            created_at=now - timedelta(minutes=10),
            hard_expires_at=now + timedelta(hours=1),
            next_reconcile_at=now - timedelta(seconds=1),
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    deleted: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider, "inspect", lambda self, _name: _observation(resource)
    )
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda self, item: deleted.append(item.id))
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    with db_session_factory() as db:
        assert db.get(ManagedSandbox, resource_id) is None
    assert report.expired == 1
    assert deleted == [resource_id]


def test_reconciler_deletes_hard_expired_resource(settings, db_session_factory, monkeypatch):
    configured = _docker_settings(settings)
    with db_session_factory() as db:
        resource = _resource(expired=True)
        db.add(resource)
        db.commit()
        resource_id = resource.id

    deleted: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider, "inspect", lambda self, _name: _observation(resource)
    )
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda self, item: deleted.append(item.id))
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        assert db.get(ManagedSandbox, resource_id) is None
    assert report.expired == 1
    assert report.deleted == 1
    assert deleted == [resource_id]


def test_reconciler_refuses_conflicting_delete_and_reclaims_only_stale_orphans(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings, sandbox_orphan_grace_seconds=300)
    with db_session_factory() as db:
        resource = _resource(desired_state="DELETED")
        db.add(resource)
        db.commit()
        resource_id = resource.id

    now = datetime.now(UTC)
    conflict = _observation(resource, resource_id="different-resource")
    stale = DockerObservation(
        resource_id="orphan-stale",
        resource_name="fw-sbx-orphan-stale",
        resource_identifier="docker-orphan-stale",
        state="RUNNING",
        labels={
            "flowweave.resource-id": "orphan-stale",
            "flowweave.created-at": str(int((now - timedelta(hours=1)).timestamp())),
        },
    )
    fresh = DockerObservation(
        resource_id="orphan-fresh",
        resource_name="fw-sbx-orphan-fresh",
        resource_identifier="docker-orphan-fresh",
        state="RUNNING",
        labels={
            "flowweave.resource-id": "orphan-fresh",
            "flowweave.created-at": str(int(now.timestamp())),
        },
    )
    reclaimed: list[str] = []
    monkeypatch.setattr(DockerSandboxProvider, "inspect", lambda self, _name: conflict)
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [stale, fresh])
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete_orphan",
        lambda self, item: reclaimed.append(item.resource_id),
    )

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    with db_session_factory() as db:
        persisted = db.get(ManagedSandbox, resource_id)
        assert persisted is not None
        assert persisted.observed_state == "ERROR"
        assert persisted.last_error_code == "SANDBOX_RESOURCE_CONFLICT"
    assert report.errors == 1
    assert report.orphans_deleted == 1
    assert reclaimed == ["orphan-stale"]


def test_reconciler_rechecks_ledger_before_deleting_orphan(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings, sandbox_orphan_grace_seconds=30)
    now = datetime.now(UTC)
    observation = DockerObservation(
        resource_id="late-ledger-resource",
        resource_name="fw-sbx-late-ledger-resource",
        resource_identifier="docker-late-ledger-resource",
        state="RUNNING",
        labels={
            "flowweave.managed": "true",
            "flowweave.manager-scope": "test-scope",
            "flowweave.resource-id": "late-ledger-resource",
            "flowweave.created-at": str(int((now - timedelta(hours=1)).timestamp())),
        },
    )
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [observation])
    deleted: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete_orphan",
        lambda self, item: deleted.append(item.resource_id),
    )

    def become_managed(_observation, _grace_seconds):
        with db_session_factory() as db:
            db.add(
                ManagedSandbox(
                    id="late-ledger-resource",
                    kind="ENVIRONMENT_SETUP",
                    owner_type="TEST_OWNER",
                    owner_id="late-owner",
                    backend="docker",
                    backend_resource_name="fw-sbx-late-ledger-resource",
                    observed_state="RUNNING",
                    image_reference="runtime:locked",
                    hard_expires_at=now + timedelta(hours=1),
                    next_reconcile_at=now + timedelta(hours=1),
                )
            )
            db.commit()
        return True

    monkeypatch.setattr(DockerSandboxProvider, "orphan_is_stale", become_managed)

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    assert report.orphans_deleted == 0
    assert deleted == []


def test_reconciler_uses_absolute_expiry_for_ephemeral_containers(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(
        settings,
        terminal_environment_backend="disabled",
        sandbox_backend="docker",
    )
    now = datetime.now(UTC)

    def ephemeral(resource_id: str, expires_at: datetime) -> DockerObservation:
        return DockerObservation(
            resource_id=resource_id,
            resource_name=f"fw-ep-gate-{resource_id}",
            resource_identifier=f"docker-{resource_id}",
            state="RUNNING",
            labels={
                "flowweave.managed": "true",
                "flowweave.manager-scope": "test-scope",
                "flowweave.resource-id": resource_id,
                "flowweave.lifecycle": "ephemeral",
                "flowweave.created-at": str(int((now - timedelta(hours=1)).timestamp())),
                "flowweave.expires-at": str(int(expires_at.timestamp())),
            },
        )

    expired = ephemeral("expired", now - timedelta(seconds=1))
    active = ephemeral("active", now + timedelta(hours=1))
    deleted: list[str] = []
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [expired, active])
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete_orphan",
        lambda self, item: deleted.append(item.resource_id),
    )

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)
        db.commit()

    assert report.orphans_deleted == 1
    assert deleted == ["expired"]


def test_reconciler_skips_docker_when_global_lock_is_held(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    bind = db_session_factory.kw["bind"]
    lock_key = "SANDBOX_RECONCILE:test-scope"
    with bind.connect() as lock_connection:
        lock_id = lock_connection.scalar(select(func.hashtextextended(lock_key, 0)))
        assert lock_id is not None
        lock_connection.scalar(select(func.pg_advisory_lock(lock_id)))
        lock_connection.commit()
        touched: list[bool] = []
        monkeypatch.setattr(
            DockerSandboxProvider,
            "list_managed",
            lambda self: touched.append(True) or [],
        )
        try:
            with settings_context(configured), db_session_factory() as db:
                report = reconcile_managed_sandboxes(db)
                db.rollback()
        finally:
            lock_connection.scalar(select(func.pg_advisory_unlock(lock_id)))
            lock_connection.commit()

    assert report == ReconcileReport()
    assert touched == []


def test_reconciler_performs_docker_io_outside_database_transaction(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    with db_session_factory() as db:
        resource = _resource()
        db.add(resource)
        db.commit()

    transaction_states: list[tuple[str, object]] = []

    def inspect_without_transaction(self, _resource_name):
        del self
        with db_session_factory() as observer:
            rows = observer.execute(
                text(
                    """
                    SELECT activity.state, activity.xact_start
                    FROM pg_stat_activity AS activity
                    WHERE EXISTS (
                        SELECT 1
                        FROM pg_locks AS locks
                        WHERE locks.pid = activity.pid
                          AND locks.locktype = 'advisory'
                          AND locks.granted
                    )
                    """
                )
            ).all()
        transaction_states.extend((str(state), xact_start) for state, xact_start in rows)
        return _observation(resource)

    monkeypatch.setattr(DockerSandboxProvider, "inspect", inspect_without_transaction)
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, item, **_kwargs: _observation(item),
    )
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    assert report.inspected == 1
    assert transaction_states
    assert all(state == "idle" and xact_start is None for state, xact_start in transaction_states)


def test_concurrent_delete_wins_over_in_flight_running_observation(
    settings, db_session_factory, monkeypatch
):
    configured = _docker_settings(settings)
    with db_session_factory() as db:
        resource = _resource()
        resource.observed_state = "PENDING"
        db.add(resource)
        db.commit()
        resource_id = resource.id

    monkeypatch.setattr(DockerSandboxProvider, "inspect", lambda self, _name: None)

    def ensure_running_after_delete(self, item, **_kwargs):
        del self
        with db_session_factory() as request_db:
            request_delete_durable(request_db, item.id)
        return _observation(item)

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running_after_delete)
    monkeypatch.setattr(DockerSandboxProvider, "list_managed", lambda self: [])

    with settings_context(configured), db_session_factory() as db:
        report = reconcile_managed_sandboxes(db)

    with db_session_factory() as db:
        persisted = db.get(ManagedSandbox, resource_id)
        assert persisted is not None
        assert persisted.desired_state == "DELETED"
        assert persisted.observed_state == "PENDING"
        assert persisted.next_reconcile_at <= datetime.now(UTC)
    assert report.deleted == 0
