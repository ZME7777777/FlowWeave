from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from starlette.websockets import WebSocketDisconnect

from flowweave.bootstrap.worker import TaskWorker
from flowweave.modules.environments.application import service as environment_service
from flowweave.modules.environments.infrastructure import docker as environment_docker
from flowweave.modules.environments.infrastructure.docker import OpenHandsBuild, PublishedImage
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.runtime.contract import OPENHANDS_PACKAGE_VERSIONS
from flowweave.shared.domain.tool_policy import OPENHANDS_SOURCE_COMMIT
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    BackgroundTask,
    EnvironmentSetupSession,
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    ManagedSandbox,
    TaskState,
)

_BASE_IMAGE = "flowweave-openhands-runtime@sha256:" + "1" * 64
_REAL_RESOLVE_SETUP_IMAGE = environment_docker.resolve_setup_image


@pytest.fixture(autouse=True)
def _resolve_test_setup_image(monkeypatch):
    monkeypatch.setattr(
        environment_docker,
        "resolve_setup_image",
        lambda _reference: (_BASE_IMAGE, "sha256:" + "1" * 64),
    )


def test_platform_setup_image_tag_is_frozen_to_content_digest(monkeypatch):
    digest = "sha256:" + "2" * 64
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            docker_controller_mode="local",
            terminal_environment_publish_timeout_seconds=600,
        ),
    )
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: (
            digest if command[-1] == "{{.Id}}" else '{"Id":"' + digest + '","RepoDigests":[]}'
        ),
    )

    assert _REAL_RESOLVE_SETUP_IMAGE("flowweave-openhands-runtime:1") == (
        "flowweave-openhands-runtime:1",
        digest,
    )


def _node_payload() -> dict[str, object]:
    return {
        "name": "环境节点",
        "description": "验证终端环境只在运行级选择",
        "inputs": [],
        "outputs": [],
        "executor": {
            "startup_prompt": "执行任务",
            "context_prompt": "",
            "timeout_seconds": 120,
            "max_iterations": 20,
        },
        "capabilities": [],
    }


def _runtime_provenance() -> dict[str, object]:
    return {
        "package_versions": dict(OPENHANDS_PACKAGE_VERSIONS),
        "source_commit": OPENHANDS_SOURCE_COMMIT,
        "source_ref": OPENHANDS_SOURCE_COMMIT,
        "source_archive_digest": (
            "a33dfae9a55732cfb6ffe0b7d5cf02b557a041bc82629df5c61459400d35c832"
        ),
        "overlays": {},
    }


def _runtime_manifest(
    *, image_digest: str = "sha256:" + "a" * 64, **values: object
) -> dict[str, object]:
    return {
        **values,
        "image_id": image_digest,
        "runtime_provenance": _runtime_provenance(),
        "build": {
            "builder": "openhands.agent_server.docker.build",
            "target": "source-minimal",
            "platform": "linux/arm64",
            "user_base_image_reference": _BASE_IMAGE,
            "user_base_image_digest": "sha256:" + "1" * 64,
            "runtime_image_digest": image_digest,
        },
        "validation": {
            "contract_check": {"status": "PASSED"},
            "tool_workspace_probe": {"status": "PASSED"},
            "security_scan": {"status": "PASSED"},
        },
    }


def _mock_setup_provider(monkeypatch):
    created: list[str] = []
    removed: list[str] = []
    fail_delete = {"value": False}

    monkeypatch.setattr(DockerSandboxProvider, "require_enabled", lambda self: None)

    def ensure_running(self, resource):
        del self
        created.append(resource.backend_resource_name)
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier=f"docker-{resource.id}",
            state="RUNNING",
            labels={"flowweave.resource-id": resource.id},
        )

    def delete(self, resource):
        del self
        if fail_delete["value"]:
            raise DomainError("SANDBOX_BACKEND_UNAVAILABLE", "unavailable", 503)
        removed.append(resource.backend_resource_name)

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    monkeypatch.setattr(DockerSandboxProvider, "delete", delete)
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.resolve_setup_container",
        lambda resource_name, *, sandbox_id, environment_id: f"immutable-{resource_name}",
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_setup_container",
        lambda resource_name,
        *,
        sandbox_id,
        environment_id,
        version_id,
        version_no,
        base_image_reference,
        base_image_digest: (
            environment_docker.publish_container(
                f"immutable-{resource_name}",
                environment_id=environment_id,
                version_id=version_id,
                version_no=version_no,
                base_image_reference=base_image_reference,
                base_image_digest=base_image_digest,
            )
        ),
    )
    return created, removed, fail_delete


def _mock_formal_publish_pipeline(monkeypatch, calls: list[str] | None = None) -> None:
    def build(**kwargs):
        if calls is not None:
            calls.append("formal-build")
        return OpenHandsBuild(
            reference="flowweave/formal-openhands-runtime:test",
            log_digest="a" * 64,
            telemetry={"duration_seconds": 1},
            target="source-minimal",
            platform=str(kwargs["platform"]),
        )

    def probe(_image_digest, *, probe_token):
        assert probe_token == "version1"
        if calls is not None:
            calls.append("probe")
        return {"agent-server": "1.42.0"}, _runtime_provenance(), "b" * 64

    monkeypatch.setattr(environment_docker, "_build_openhands_runtime", build)
    monkeypatch.setattr(environment_docker, "_probe_runtime_image", probe)


def test_legacy_container_cleanup_requires_matching_ownership_labels(monkeypatch):
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(docker_binary="docker", docker_controller_mode="local"),
    )

    def owned(command, **_kwargs):
        commands.append(command)
        if command[1] == "inspect":
            return (
                '{"Id":"immutable-id","Config":{"Labels":{'
                '"flowweave.managed":"terminal-environment",'
                '"flowweave.environment":"environment-1"}}}'
            )
        return ""

    monkeypatch.setattr(environment_docker, "_run", owned)
    environment_docker.remove_legacy_setup_container("legacy-name", environment_id="environment-1")
    assert commands[-1] == ["docker", "rm", "--force", "immutable-id"]

    commands.clear()
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: (
            commands.append(command)
            or '{"Id":"other-id","Config":{"Labels":{'
            '"flowweave.managed":"terminal-environment",'
            '"flowweave.environment":"another-environment"}}}'
        ),
    )

    with pytest.raises(DomainError) as caught:
        environment_docker.remove_legacy_setup_container(
            "legacy-name", environment_id="environment-1"
        )
    assert caught.value.code == "ENVIRONMENT_CONTAINER_OWNERSHIP_MISMATCH"
    assert [command[1] for command in commands] == ["inspect"]


def test_managed_setup_resolution_fails_closed_on_ownership_mismatch(monkeypatch):
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            sandbox_manager_scope="expected-scope",
            docker_controller_mode="local",
        ),
    )
    inspected: list[tuple[str, str, str]] = []

    def ownership_mismatch(docker_binary, resource_name, expected_resource_id, **_kwargs):
        inspected.append((docker_binary, resource_name, expected_resource_id))
        raise environment_docker.DockerOwnershipError("wrong manager scope")

    monkeypatch.setattr(environment_docker, "inspect_owned_container", ownership_mismatch)

    with pytest.raises(DomainError) as caught:
        environment_docker.resolve_setup_container(
            "reused-name",
            sandbox_id="sandbox-expected",
            environment_id="environment-1",
        )

    assert caught.value.code == "ENVIRONMENT_CONTAINER_OWNERSHIP_MISMATCH"
    assert inspected == [("docker", "reused-name", "sandbox-expected")]


def test_image_cleanup_removes_only_the_expected_tag(monkeypatch):
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(docker_binary="docker", docker_controller_mode="local"),
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["image", "inspect"]:
            return '{"Id":"sha256:expected"}'
        return ""

    monkeypatch.setattr(environment_docker, "_run", fake_run)
    environment_docker.remove_image(
        "flowweave/environment-one:v1", expected_digest="sha256:expected"
    )

    assert commands == [
        [
            "docker",
            "image",
            "inspect",
            "flowweave/environment-one:v1",
            "--format",
            "{{json .}}",
        ],
        ["docker", "image", "rm", "flowweave/environment-one:v1"],
    ]


def test_image_cleanup_refuses_a_retargeted_tag(monkeypatch):
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(docker_binary="docker", docker_controller_mode="local"),
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: (commands.append(command) or '{"Id":"sha256:replacement"}'),
    )

    with pytest.raises(DomainError) as caught:
        environment_docker.remove_image(
            "flowweave/environment-one:v1", expected_digest="sha256:expected"
        )

    assert caught.value.code == "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH"
    assert [command[1:3] for command in commands] == [["image", "inspect"]]


def test_publish_preserves_container_files_before_commit(monkeypatch):
    calls: list[str] = []
    _mock_formal_publish_pipeline(monkeypatch, calls)

    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            terminal_environment_publish_timeout_seconds=60,
            sandbox_manager_scope="test-scope",
        ),
    )
    monkeypatch.setattr(
        environment_docker,
        "_inspect_commands",
        lambda container_id: calls.append("inspect") or {"agent-server": "1.0"},
    )
    monkeypatch.setattr(
        environment_docker,
        "_inspect_runtime_provenance",
        lambda _container_id: _runtime_provenance(),
    )
    monkeypatch.setattr(
        environment_docker,
        "container_diff",
        lambda container_id: calls.append("scan")
        or ["A /root/.ssh/id_ed25519", "A /root/.lark-cli/token.json"],
    )

    committed = False
    wrapped = False

    def fake_run(command, **kwargs):
        nonlocal committed, wrapped
        if "commit" in command:
            calls.append("commit")
            committed = True
            return "sha256:" + "c" * 64
        if command[1] == "build":
            calls.append("wrap")
            wrapped = True
            return ""
        if command[1:3] == ["image", "inspect"]:
            reference = command[3]
            if reference == "flowweave/environment-environment-1:v1-version1" and not wrapped:
                raise DomainError(
                    "ENVIRONMENT_DOCKER_FAILED",
                    "missing",
                    502,
                    {"detail": "No such image"},
                )
            if reference.endswith("-base:v1-version1"):
                return '{"Id":"sha256:' + "c" * 64 + '","Architecture":"arm64","Os":"linux"}'
            if reference == "flowweave/formal-openhands-runtime:test":
                return '{"Id":"sha256:' + "d" * 64 + '"}'
            return (
                '{"Id":"sha256:' + "e" * 64 + '","Architecture":"arm64","Os":"linux",'
                '"Config":{"Labels":{'
                '"flowweave.managed":"environment-image",'
                '"flowweave.manager-scope":"test-scope",'
                '"flowweave.environment-id":"environment-1",'
                '"flowweave.environment-version-id":"version-1",'
                '"flowweave.environment-version-no":"1"}}}'
            )
        raise AssertionError(command)

    monkeypatch.setattr(environment_docker, "_run", fake_run)
    published = environment_docker.publish_container(
        "container-1",
        environment_id="environment-1",
        version_id="version-1",
        version_no=1,
        base_image_reference="python@sha256:" + "1" * 64,
        base_image_digest="sha256:" + "1" * 64,
    )

    assert calls == ["scan", "commit", "formal-build", "wrap", "probe"]
    assert published.reference == "flowweave/environment-environment-1:v1-version1"
    assert published.manifest["filesystem_change_count"] == 2


def test_publish_refuses_a_tag_owned_by_another_version(monkeypatch):
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            terminal_environment_publish_timeout_seconds=60,
            sandbox_manager_scope="test-scope",
        ),
    )
    external_io: list[str] = []
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: (
            external_io.append(command[1])
            or '{"Id":"sha256:occupied","Config":{"Labels":{'
            '"flowweave.managed":"environment-image",'
            '"flowweave.manager-scope":"another-scope"}}}'
        ),
    )
    monkeypatch.setattr(
        environment_docker,
        "_inspect_commands",
        lambda _container_id: pytest.fail("container must not be inspected"),
    )

    with pytest.raises(DomainError) as caught:
        environment_docker.publish_container(
            "container-1",
            environment_id="environment-1",
            version_id="version-1",
            version_no=1,
            base_image_reference="python@sha256:" + "1" * 64,
            base_image_digest="sha256:" + "1" * 64,
        )

    assert caught.value.code == "ENVIRONMENT_IMAGE_TAG_CONFLICT"
    assert external_io == ["image"]


def test_publish_fails_if_docker_drops_image_ownership_labels(monkeypatch):
    _mock_formal_publish_pipeline(monkeypatch)
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            terminal_environment_publish_timeout_seconds=60,
            sandbox_manager_scope="test-scope",
        ),
    )
    monkeypatch.setattr(environment_docker, "_inspect_commands", lambda _container_id: {})
    monkeypatch.setattr(
        environment_docker,
        "_inspect_runtime_provenance",
        lambda _container_id: _runtime_provenance(),
    )
    monkeypatch.setattr(environment_docker, "container_diff", lambda _container_id: [])
    committed = False
    wrapped = False

    def fake_run(command, **_kwargs):
        nonlocal committed, wrapped
        if "commit" in command:
            committed = True
            return "sha256:" + "c" * 64
        if command[1] == "build":
            wrapped = True
            return ""
        if command[1:3] == ["image", "inspect"]:
            reference = command[3]
            if reference == "flowweave/environment-environment-1:v1-version1" and not wrapped:
                raise DomainError(
                    "ENVIRONMENT_DOCKER_FAILED", "missing", 502, {"detail": "No such image"}
                )
            if reference.endswith("-base:v1-version1"):
                return '{"Id":"sha256:' + "c" * 64 + '","Architecture":"arm64","Os":"linux"}'
            if reference == "flowweave/formal-openhands-runtime:test":
                return '{"Id":"sha256:' + "d" * 64 + '"}'
            return '{"Id":"sha256:' + "e" * 64 + '","Config":{"Labels":{}}}'
        raise AssertionError(command)

    monkeypatch.setattr(environment_docker, "_run", fake_run)

    with pytest.raises(DomainError) as caught:
        environment_docker.publish_container(
            "container-1",
            environment_id="environment-1",
            version_id="version-1",
            version_no=1,
            base_image_reference="python@sha256:" + "1" * 64,
            base_image_digest="sha256:" + "1" * 64,
        )

    assert caught.value.code == "ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH"


def test_publish_allows_authentication_files(monkeypatch):
    _mock_formal_publish_pipeline(monkeypatch)
    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            terminal_environment_publish_timeout_seconds=60,
            sandbox_manager_scope="test-scope",
        ),
    )
    monkeypatch.setattr(environment_docker, "_inspect_commands", lambda _container_id: {})
    monkeypatch.setattr(
        environment_docker,
        "_inspect_runtime_provenance",
        lambda _container_id: _runtime_provenance(),
    )
    monkeypatch.setattr(
        environment_docker,
        "container_diff",
        lambda _container_id: [
            "A /root/.lark-cli/token.json",
            "A /root/.ssh/id_ed25519",
            "A /root/.config/feishu/session.json",
        ],
    )
    committed = False
    wrapped = False

    def fake_run(command, **_kwargs):
        nonlocal committed, wrapped
        if "commit" in command:
            committed = True
            return "sha256:" + "c" * 64
        if command[1] == "build":
            wrapped = True
            return ""
        if command[1:3] == ["image", "inspect"]:
            reference = command[3]
            if reference == "flowweave/environment-environment-1:v1-version1" and not wrapped:
                raise DomainError(
                    "ENVIRONMENT_DOCKER_FAILED", "missing", 502, {"detail": "No such image"}
                )
            if reference.endswith("-base:v1-version1"):
                return '{"Id":"sha256:' + "c" * 64 + '","Architecture":"arm64","Os":"linux"}'
            if reference == "flowweave/formal-openhands-runtime:test":
                return '{"Id":"sha256:' + "d" * 64 + '"}'
            return (
                '{"Id":"sha256:' + "e" * 64 + '","Architecture":"arm64","Os":"linux",'
                '"Config":{"Labels":{'
                '"flowweave.managed":"environment-image",'
                '"flowweave.manager-scope":"test-scope",'
                '"flowweave.environment-id":"environment-1",'
                '"flowweave.environment-version-id":"version-1",'
                '"flowweave.environment-version-no":"1"}}}'
            )
        raise AssertionError(command)

    monkeypatch.setattr(environment_docker, "_run", fake_run)

    published = environment_docker.publish_container(
        "container-1",
        environment_id="environment-1",
        version_id="version-1",
        version_no=1,
        base_image_reference="python@sha256:" + "1" * 64,
        base_image_digest="sha256:" + "1" * 64,
    )

    assert committed is True
    assert published.manifest["filesystem_change_count"] == 3


def test_terminal_environment_publish_does_not_bind_nodes(client, worker_container, monkeypatch):
    created_sandboxes, removed, _fail_delete = _mock_setup_provider(monkeypatch)
    published_container_ids: list[str] = []

    def publish(
        container_id,
        *,
        environment_id,
        version_id,
        version_no,
        base_image_reference,
        base_image_digest,
    ):
        assert version_id
        assert base_image_reference == _BASE_IMAGE
        assert base_image_digest == "sha256:" + "1" * 64
        published_container_ids.append(container_id)
        return PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + "a" * 64,
            manifest=_runtime_manifest(
                schema_version=1,
                commands={"python": "Python 3.13", "lark-cli": "1.0.84"},
            ),
        )

    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_container",
        publish,
    )

    created = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "飞书工具环境",
            "description": "安装交互式 CLI",
        },
    )
    assert created.status_code == 201, created.text
    environment = created.json()
    assert environment["versions"] == []
    assert "base_image" not in environment
    assert "base_image_digest" not in environment

    rejected_override = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "不允许覆盖启动镜像",
            "description": "",
            "base_image": _BASE_IMAGE,
        },
    )
    assert rejected_override.status_code == 422, rejected_override.text

    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    )
    assert setup.status_code == 201, setup.text
    session = setup.json()
    assert session["state"] == "RUNNING"
    assert len(created_sandboxes) == 1

    published = client.post(f"/api/v1/environment-setup-sessions/{session['id']}/publish")
    assert published.status_code == 201, published.text
    version = published.json()
    assert version["state"] == "READY"
    assert version["image_digest"] == "sha256:" + "a" * 64
    assert version["manifest"]["commands"]["lark-cli"] == "1.0.84"
    assert published_container_ids == [f"immutable-{created_sandboxes[0]}"]
    assert removed == []
    assert TaskWorker(worker_container)._run_once_sync() is True
    assert removed == created_sandboxes

    retried = client.post(f"/api/v1/environment-setup-sessions/{session['id']}/publish")
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] == version["id"]
    assert removed == created_sandboxes

    node = client.post("/api/v1/node-assets", json=_node_payload())
    assert node.status_code == 201, node.text
    asset = node.json()
    assert "environment_version_id" not in asset
    assert "environment_version" not in asset

    listed_version = client.get(f"/api/v1/terminal-environments/{environment['id']}").json()[
        "versions"
    ][0]
    assert listed_version["run_reference_count"] == 0
    assert listed_version["reference_count"] == 0
    assert "node_reference_count" not in listed_version

    deleted_version = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{version['id']}"
    )
    assert deleted_version.status_code == 204, deleted_version.text
    deleted_environment = client.delete(f"/api/v1/terminal-environments/{environment['id']}")
    assert deleted_environment.status_code == 204, deleted_environment.text


def test_setup_allocation_starts_after_caller_transaction_is_released(client, monkeypatch):
    _mock_setup_provider(monkeypatch)
    original = environment_service.sandboxes.create_setup_sandbox
    transaction_states: list[bool] = []

    def inspect_boundary(db, **kwargs):
        transaction_states.append(db.in_transaction())
        return original(db, **kwargs)

    monkeypatch.setattr(
        environment_service.sandboxes,
        "create_setup_sandbox",
        inspect_boundary,
    )
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "配置短事务环境",
            "description": "",
        },
    ).json()

    created = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    )

    assert created.status_code == 201, created.text
    assert transaction_states == [False]


def test_setup_global_capacity_applies_across_environments(client, monkeypatch):
    _mock_setup_provider(monkeypatch)
    monkeypatch.setattr(
        environment_service,
        "get_settings",
        lambda: SimpleNamespace(
            terminal_environment_setup_image="flowweave-openhands-runtime:1",
            terminal_environment_max_active_sessions=1,
            terminal_environment_session_ttl_seconds=14_400,
        ),
    )
    environments = []
    for suffix in ("one", "two"):
        response = client.post(
            "/api/v1/terminal-environments",
            json={
                "name": f"global-capacity-{suffix}",
                "description": "",
            },
        )
        assert response.status_code == 201, response.text
        environments.append(response.json())

    first = client.post(
        f"/api/v1/terminal-environments/{environments[0]['id']}/setup-sessions", json={}
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/terminal-environments/{environments[1]['id']}/setup-sessions", json={}
    )

    assert second.status_code == 429, second.text
    error = second.json()["error"]
    assert error["code"] == "ENVIRONMENT_SETUP_CAPACITY_EXCEEDED"
    assert error["details"] == {"active_sessions": 1, "max_active_sessions": 1}


def test_publish_docker_io_holds_no_database_transaction(client, db_session_factory, monkeypatch):
    _mock_setup_provider(monkeypatch)
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "发布短事务环境",
            "description": "",
        },
    ).json()
    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    ).json()
    transaction_states: list[tuple[str, object]] = []

    def publish_without_transaction(
        _resource_name, *, sandbox_id, environment_id, version_id, version_no, **_base
    ):
        del sandbox_id
        assert version_id
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
        return PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + "e" * 64,
            manifest=_runtime_manifest(image_digest="sha256:" + "e" * 64, schema_version=1),
        )

    monkeypatch.setattr(
        environment_docker,
        "publish_setup_container",
        publish_without_transaction,
    )

    published = client.post(f"/api/v1/environment-setup-sessions/{setup['id']}/publish")

    assert published.status_code == 201, published.text
    assert transaction_states
    assert all(state == "idle" and xact_start is None for state, xact_start in transaction_states)


def test_late_publish_result_after_cancel_is_cleaned_without_becoming_ready(
    client, db_session_factory, monkeypatch
):
    _mock_setup_provider(monkeypatch)
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "发布竞态环境",
            "description": "",
        },
    ).json()
    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    ).json()
    digest = "sha256:" + "f" * 64

    def publish_after_cancel(
        _resource_name, *, sandbox_id, environment_id, version_id, version_no, **_base
    ):
        del sandbox_id
        assert version_id
        with db_session_factory() as concurrent_db:
            environment_service.stop_setup_session(concurrent_db, setup["id"])
        return PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest=digest,
            manifest=_runtime_manifest(image_digest=digest, schema_version=1),
        )

    monkeypatch.setattr(
        environment_docker,
        "publish_setup_container",
        publish_after_cancel,
    )

    response = client.post(f"/api/v1/environment-setup-sessions/{setup['id']}/publish")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ENVIRONMENT_PUBLISH_STALE"
    with db_session_factory() as db:
        persisted = db.get(EnvironmentSetupSession, setup["id"])
        assert persisted is not None
        assert persisted.state == "CANCELLED"
        assert persisted.published_version_id is not None
        version = db.get(EnvironmentVersion, persisted.published_version_id)
        assert version is not None
        assert version.state == "FAILED"
        assert version.image_reference == ""
        assert not list(
            db.scalars(
                select(EnvironmentVersion).where(
                    EnvironmentVersion.environment_id == environment["id"],
                    EnvironmentVersion.state == "READY",
                )
            )
        )
        cleanup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_ENVIRONMENT_IMAGE",
                BackgroundTask.aggregate_id == version.id,
            )
        )
        assert cleanup is not None
        assert cleanup.payload_json == {
            "environment_id": environment["id"],
            "version_id": version.id,
            "version_no": 1,
            "image_reference": f"flowweave/environment-{environment['id']}:v1",
            "image_digest": digest,
        }


def test_environment_version_run_reference_is_reported_and_blocks_deletion(
    client, db_session_factory
):
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "运行占用环境",
            "description": "",
        },
    ).json()
    with db_session_factory() as db:
        version = EnvironmentVersion(
            environment_id=environment["id"],
            version_no=1,
            state="READY",
            image_reference="flowweave/run-reference:v1",
            image_digest="sha256:" + "b" * 64,
        )
        flow = FlowDefinition(
            name="environment-run-reference",
            description="",
            lark_root_folder_url="https://example.feishu.cn/drive/folder/root",
        )
        db.add_all([version, flow])
        db.flush()
        db.add(
            FlowRun(
                flow_definition_id=flow.id,
                run_no=1,
                name="environment run",
                environment_version_id=version.id,
            )
        )
        db.commit()
        version_id = version.id

    listed_version = client.get(f"/api/v1/terminal-environments/{environment['id']}").json()[
        "versions"
    ][0]
    assert listed_version["run_reference_count"] == 1
    assert listed_version["reference_count"] == 1
    assert "node_reference_count" not in listed_version

    blocked = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{version_id}"
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ENVIRONMENT_VERSION_IN_USE"
    assert blocked.json()["error"]["details"]["run_reference_count"] == 1

    blocked_environment = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}"
    )
    assert blocked_environment.status_code == 409, blocked_environment.text
    error = blocked_environment.json()["error"]
    assert error["code"] == "ENVIRONMENT_IN_USE"
    assert error["message"] == "The terminal environment is referenced by a Snapshot or FlowRun"
    assert error["details"] == {
        "flow_run_reference_count": 1,
        "snapshot_reference_count": 0,
    }


def test_delete_unused_versions_clears_provenance_and_preserves_version_high_watermark(
    client, db_session_factory, worker_container, monkeypatch
):
    _created_sandboxes, _removed_sandboxes, _fail_delete = _mock_setup_provider(monkeypatch)
    removed_images: list[str] = []
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_container",
        lambda container_id, *, environment_id, version_id, version_no, **_base: PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + str(version_no) * 64,
            manifest=_runtime_manifest(
                image_digest="sha256:" + str(version_no) * 64,
                schema_version=1,
                container_id=container_id,
                version_id=version_id,
            ),
        ),
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.remove_image",
        lambda reference, *, expected_digest, **_ownership: removed_images.append(
            (reference, expected_digest)
        ),
    )

    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "版本清理环境",
            "description": "",
        },
    ).json()

    first_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions", json={}
    ).json()
    first = client.post(f"/api/v1/environment-setup-sessions/{first_session['id']}/publish").json()
    assert TaskWorker(worker_container)._run_once_sync() is True
    second_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={"base_version_id": first["id"]},
    ).json()
    second = client.post(
        f"/api/v1/environment-setup-sessions/{second_session['id']}/publish"
    ).json()
    assert TaskWorker(worker_container)._run_once_sync() is True
    assert (first["version_no"], second["version_no"]) == (1, 2)

    deleted = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{first['id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert removed_images == []
    assert TaskWorker(worker_container)._run_once_sync() is True
    assert removed_images == [(first["image_reference"], first["image_digest"])]
    with db_session_factory() as db:
        assert db.get(EnvironmentVersion, first["id"]) is None
        persisted_second = db.get(EnvironmentVersion, second["id"])
        persisted_session = db.get(EnvironmentSetupSession, second_session["id"])
        assert persisted_second is not None and persisted_second.parent_version_id is None
        assert persisted_session is not None and persisted_session.base_version_id is None

    deleted = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{second['id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert TaskWorker(worker_container)._run_once_sync() is True
    third_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions", json={}
    ).json()
    third = client.post(f"/api/v1/environment-setup-sessions/{third_session['id']}/publish").json()
    assert third["version_no"] == 3


def test_environment_delete_removes_versions_and_enqueues_image_cleanup(client, db_session_factory):
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "环境删除镜像回收",
            "description": "",
        },
    ).json()
    digest = "sha256:" + "8" * 64
    reference = f"flowweave/environment-{environment['id']}:v1-version"
    with db_session_factory() as db:
        version = EnvironmentVersion(
            environment_id=environment["id"],
            version_no=1,
            state="READY",
            image_reference=reference,
            image_digest=digest,
        )
        db.add(version)
        db.commit()
        version_id = version.id

    response = client.delete(f"/api/v1/terminal-environments/{environment['id']}")

    assert response.status_code == 204, response.text
    with db_session_factory() as db:
        assert db.get(EnvironmentVersion, version_id) is None
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_ENVIRONMENT_IMAGE",
                BackgroundTask.aggregate_id == version_id,
            )
        )
        assert task is not None
        assert task.payload_json == {
            "environment_id": environment["id"],
            "version_id": version_id,
            "version_no": 1,
            "image_reference": reference,
            "image_digest": digest,
        }
        credential_task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_ENVIRONMENT_CREDENTIALS",
                BackgroundTask.aggregate_id == environment["id"],
            )
        )
        assert credential_task is not None
        assert credential_task.payload_json == {"environment_id": environment["id"]}


def test_environment_credential_cleanup_waits_for_live_sandbox(
    client, db_session_factory, monkeypatch
):
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "凭据卷最终门禁",
            "description": "",
        },
    ).json()
    client.delete(f"/api/v1/terminal-environments/{environment['id']}")
    touched: list[str] = []
    monkeypatch.setattr(
        "flowweave.modules.sandboxes.public.environment_has_live_sandbox",
        lambda _db, *, environment_id: True,
    )
    monkeypatch.setattr(
        "flowweave.modules.sandboxes.public.delete_environment_credentials",
        lambda environment_id: touched.append(environment_id),
    )

    with db_session_factory() as db, pytest.raises(DomainError) as caught:
        environment_service.process_cleanup_environment_credentials(
            db,
            environment["id"],
            environment_service.Lease("unused", "worker", 1),
        )

    assert caught.value.code == "ENVIRONMENT_CREDENTIALS_STILL_REFERENCED"
    assert touched == []


def test_image_cleanup_final_gate_does_not_touch_referenced_image(
    client, db_session_factory, monkeypatch
):
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "镜像最终门禁",
            "description": "",
        },
    ).json()
    digest = "sha256:" + "7" * 64
    reference = f"flowweave/environment-{environment['id']}:v1-version"
    touched: list[str] = []
    monkeypatch.setattr(
        environment_docker,
        "remove_image",
        lambda image_reference, **_kwargs: touched.append(image_reference),
    )
    with db_session_factory() as db:
        version = EnvironmentVersion(
            environment_id=environment["id"],
            version_no=1,
            state="READY",
            image_reference=reference,
            image_digest=digest,
        )
        db.add(version)
        db.commit()
        version_id = version.id

    with db_session_factory() as db, pytest.raises(DomainError) as caught:
        environment_service.process_cleanup_environment_image(
            db,
            {
                "environment_id": environment["id"],
                "version_id": version_id,
                "version_no": 1,
                "image_reference": reference,
                "image_digest": digest,
            },
            environment_service.Lease("unused", "worker", 1),
        )

    assert caught.value.code == "ENVIRONMENT_IMAGE_STILL_REFERENCED"
    assert touched == []


def test_permanent_image_ownership_conflict_is_not_recovered(db_session_factory):
    with db_session_factory() as db:
        task = BackgroundTask(
            task_type="CLEANUP_ENVIRONMENT_IMAGE",
            aggregate_type="ENVIRONMENT_VERSION",
            aggregate_id="00000000-0000-0000-0000-000000000001",
            idempotency_key="permanent-image-ownership-conflict",
            state=TaskState.DEAD,
            last_error=("ENVIRONMENT_IMAGE_OWNERSHIP_MISMATCH: ownership labels changed"),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        assert environment_service.recover_environment_cleanup_tasks(db) == 0

    with db_session_factory() as db:
        persisted = db.get(BackgroundTask, task_id)
        assert persisted is not None
        assert persisted.state == TaskState.DEAD


def test_setup_session_reports_disabled_backend(client):
    created = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "关闭后端环境",
            "description": "",
        },
    )
    assert created.status_code == 201, created.text

    setup = client.post(
        f"/api/v1/terminal-environments/{created.json()['id']}/setup-sessions",
        json={},
    )
    assert setup.status_code == 503, setup.text
    assert setup.json()["error"]["code"] == "ENVIRONMENT_BACKEND_DISABLED"


def test_terminal_websocket_resolves_application_container(client):
    """WebSocket dependencies must resolve from a WebSocket scope, not Request."""

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/environment-setup-sessions/00000000-0000-0000-0000-000000000000/terminal"
        ):
            pass
    assert caught.value.code == 4404


def test_setup_terminal_uses_session_scoped_persistent_tmux(client, monkeypatch):
    _mock_setup_provider(monkeypatch)
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "持久配置终端",
            "description": "",
        },
    ).json()
    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    ).json()
    opened: list[dict[str, object]] = []

    class Terminal:
        def read(self):
            return b"", True

        def write(self, _content):
            return None

        def resize(self, _rows, _columns):
            return None

        def close(self):
            return None

    def open_terminal(resource_name, **kwargs):
        opened.append({"resource_name": resource_name, **kwargs})
        return Terminal()

    monkeypatch.setattr(environment_docker, "open_managed_terminal", open_terminal)

    terminal_url = f"/api/v1/environment-setup-sessions/{setup['id']}/terminal?rows=30&columns=120"
    # Closing the first WebSocket disposes only its attachment. Reopening the
    # view must resolve to the exact same tmux session rather than a new shell.
    for _ in range(2):
        with client.websocket_connect(terminal_url):
            pass

    assert len(opened) == 2
    assert {item["session_name"] for item in opened} == {f"flowweave-setup-{setup['id']}"}
    assert all(item["rows"] == 30 for item in opened)
    assert all(item["columns"] == 120 for item in opened)


def test_terminal_opens_bash(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    commands: list[list[str]] = []
    events: list[tuple[str, int, int] | tuple[str]] = []
    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker"))
    monkeypatch.setattr(docker.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(docker.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        docker,
        "resize_terminal",
        lambda master, rows, columns: events.append(("resize", rows, columns)),
    )

    process = object()

    def popen(command, **_kwargs):
        events.append(("popen",))
        commands.append(command)
        return process

    monkeypatch.setattr(docker.subprocess, "Popen", popen)

    master, opened = docker.open_terminal("setup-container-1")

    assert master == 10
    assert opened is process
    assert events == [("resize", 24, 80), ("popen",)]
    assert commands == [
        [
            "docker",
            "exec",
            "-it",
            "-e",
            "TERM=xterm-256color",
            "setup-container-1",
            "bash",
            "-c",
            (
                r"exec 3<<<'PS1=flowweave@\h:\w\$ '; "
                r"exec bash --noprofile --rcfile /dev/fd/3 -i"
            ),
        ]
    ]


def test_agent_runtime_terminal_starts_in_persistent_project_root(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        docker,
        "get_settings",
        lambda: SimpleNamespace(docker_binary="docker", sandbox_manager_scope="test-scope"),
    )
    monkeypatch.setattr(docker, "controller_is_remote", lambda _settings: False)
    monkeypatch.setattr(
        docker, "inspect_owned_container", lambda *_args, **_kwargs: "agent-container-id"
    )
    monkeypatch.setattr(
        docker,
        "open_terminal",
        lambda container_id, **kwargs: calls.append({"container_id": container_id, **kwargs})
        or (10, object()),
    )

    opened = docker.open_managed_terminal(
        "agent-runtime",
        resource_id="resource-id",
        session_name="agent-workspace",
    )

    assert opened.master == 10
    assert calls == [
        {
            "container_id": "agent-container-id",
            "session_name": "agent-workspace",
            "working_dir": "/runtime/workspace/project",
            "rows": 24,
            "columns": 80,
        }
    ]


def test_terminal_can_attach_to_persistent_tmux_session(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    commands: list[list[str]] = []
    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker"))
    monkeypatch.setattr(docker.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(docker.os, "close", lambda _fd: None)
    monkeypatch.setattr(docker, "resize_terminal", lambda *_args: None)
    process = object()

    def popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(docker.subprocess, "Popen", popen)

    master, opened = docker.open_terminal(
        "runtime-container-1",
        session_name="FlowWeave/conversation:123",
        rows=31,
        columns=121,
    )

    assert master == 10
    assert opened is process
    assert commands == [
        [
            "docker",
            "exec",
            "-it",
            "-e",
            "TERM=xterm-256color",
            "runtime-container-1",
            "bash",
            "-c",
            (
                'session="$1"; shell_script="$2"; columns="$3"; rows="$4"; '
                'if ! tmux has-session -t "$session" 2>/dev/null; then '
                'tmux new-session -d -x "$columns" -y "$rows" -s "$session" '
                'bash -c "$shell_script" '
                '|| tmux has-session -t "$session"; fi; '
                'tmux set-option -t "$session" mouse on; '
                'tmux set-option -t "$session" status off; '
                'tmux set-option -w -t "$session": window-size latest; '
                'exec tmux attach-session -t "$session"'
            ),
            "--",
            "flowweave-conversation-123",
            (
                r"exec 3<<<'PS1=flowweave@\h:\w\$ '; "
                r"exec bash --noprofile --rcfile /dev/fd/3 -i"
            ),
            "121",
            "31",
        ]
    ]


def test_terminal_resize_notifies_docker_exec_after_ioctl(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    events: list[tuple[object, ...]] = []

    class Process:
        def poll(self):
            return None

        def send_signal(self, value):
            events.append(("signal", value))

    monkeypatch.setattr(
        "fcntl.ioctl",
        lambda master, operation, size: events.append(("ioctl", master, operation, size)),
    )

    docker.resize_terminal(10, 30, 140, Process())

    assert events[0][0] == "ioctl"
    assert events[1] == ("signal", docker.signal.SIGWINCH)


def test_expired_setup_session_is_reclaimed_before_starting_another(
    client, db_session_factory, worker_container, monkeypatch
):
    created_sandboxes, removed, _fail_delete = _mock_setup_provider(monkeypatch)

    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "过期回收环境",
            "description": "验证过期容器不会阻塞后续配置",
        },
    ).json()
    first = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    )
    assert first.status_code == 201, first.text

    first_id = first.json()["id"]
    with db_session_factory() as db:
        session = db.get(EnvironmentSetupSession, first_id)
        assert session is not None
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    second = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first_id
    assert removed == []
    assert TaskWorker(worker_container)._run_once_sync() is True
    assert removed == [created_sandboxes[0]]

    with db_session_factory() as db:
        expired = db.get(EnvironmentSetupSession, first_id)
        assert expired is not None
        assert expired.state == "EXPIRED"
        assert expired.container_id == ""
        active = list(
            db.scalars(
                select(EnvironmentSetupSession).where(
                    EnvironmentSetupSession.environment_id == environment["id"],
                    EnvironmentSetupSession.state == "RUNNING",
                )
            )
        )
        assert [item.id for item in active] == [second.json()["id"]]


def test_setup_cleanup_clears_locator_after_provider_reconciles_sandbox_first(
    client, db_session_factory, worker_container, monkeypatch
):
    _created_sandboxes, removed, _fail_delete = _mock_setup_provider(monkeypatch)
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "Sandbox 先回收环境",
            "description": "验证 Provider 与配置清理任务的竞态",
        },
    ).json()
    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    ).json()

    with db_session_factory() as db:
        item = db.get(EnvironmentSetupSession, setup["id"])
        assert item is not None and item.sandbox_id is not None
        sandbox_id = item.sandbox_id
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    with db_session_factory() as db:
        assert environment_service.expire_setup_sessions(db) == 1
        sandbox = db.get(ManagedSandbox, sandbox_id)
        assert sandbox is not None
        db.delete(sandbox)
        db.commit()

    assert TaskWorker(worker_container)._run_once_sync() is True
    with db_session_factory() as db:
        item = db.get(EnvironmentSetupSession, setup["id"])
        assert item is not None
        assert item.state == "EXPIRED"
        assert item.sandbox_id is None
        assert item.container_id == ""
    assert removed == []
    deleted = client.delete(f"/api/v1/terminal-environments/{environment['id']}")
    assert deleted.status_code == 204, deleted.text


def test_cleanup_recovery_requeues_succeeded_task_with_stale_container_locator(
    client, db_session_factory, worker_container, monkeypatch
):
    _created_sandboxes, removed, _fail_delete = _mock_setup_provider(monkeypatch)
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "历史清理后置条件修复",
            "description": "验证错误成功任务会自动恢复",
        },
    ).json()
    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    ).json()

    with db_session_factory() as db:
        item = db.get(EnvironmentSetupSession, setup["id"])
        assert item is not None and item.sandbox_id is not None
        sandbox_id = item.sandbox_id
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    with db_session_factory() as db:
        assert environment_service.expire_setup_sessions(db) == 1
        item = db.get(EnvironmentSetupSession, setup["id"])
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_SETUP_CONTAINER",
                BackgroundTask.aggregate_id == setup["id"],
            )
        )
        sandbox = db.get(ManagedSandbox, sandbox_id)
        assert item is not None and task is not None and sandbox is not None
        db.delete(sandbox)
        task.state = TaskState.SUCCEEDED
        db.commit()

    with db_session_factory() as db:
        assert environment_service.recover_environment_cleanup_tasks(db) == 1
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_SETUP_CONTAINER",
                BackgroundTask.aggregate_id == setup["id"],
            )
        )
        assert task is not None
        assert task.state == TaskState.RETRY
        assert task.last_error == "RESOURCE_CLEANUP_POSTCONDITION_MISSING"

    assert TaskWorker(worker_container)._run_once_sync() is True
    with db_session_factory() as db:
        item = db.get(EnvironmentSetupSession, setup["id"])
        assert item is not None and item.container_id == ""
    assert removed == []
    deleted = client.delete(f"/api/v1/terminal-environments/{environment['id']}")
    assert deleted.status_code == 204, deleted.text


def test_setup_cleanup_failure_retries_without_losing_container_ownership(
    client, db_session_factory, worker_container, monkeypatch
):
    created_sandboxes, removed, fail_delete = _mock_setup_provider(monkeypatch)
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_container",
        lambda container_id, *, environment_id, version_id, version_no, **_base: PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + "d" * 64,
            manifest=_runtime_manifest(
                image_digest="sha256:" + "d" * 64,
                schema_version=1,
                container_id=container_id,
                version_id=version_id,
            ),
        ),
    )
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "清理重试环境",
            "description": "",
        },
    ).json()
    session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions", json={}
    ).json()
    published = client.post(f"/api/v1/environment-setup-sessions/{session['id']}/publish")
    assert published.status_code == 201, published.text

    fail_delete["value"] = True
    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    with db_session_factory() as db:
        persisted = db.get(EnvironmentSetupSession, session["id"])
        task = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.aggregate_id == session["id"],
                BackgroundTask.task_type == "CLEANUP_SETUP_CONTAINER",
            )
        )
        assert persisted is not None and persisted.container_id == created_sandboxes[0]
        assert task is not None and task.state == "RETRY"
        task.available_at = datetime.now(UTC)
        db.commit()

    fail_delete["value"] = False
    assert worker._run_once_sync() is True
    with db_session_factory() as db:
        persisted = db.get(EnvironmentSetupSession, session["id"])
        assert persisted is not None and persisted.container_id == ""
    assert removed == created_sandboxes
