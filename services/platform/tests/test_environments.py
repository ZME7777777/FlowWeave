from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from flowweave.modules.environments.infrastructure import docker as environment_docker
from flowweave.modules.environments.infrastructure.docker import PublishedImage, _sensitive_paths
from flowweave.shared.models import (
    EnvironmentSetupSession,
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
)


def _node_payload(environment_version_id: str | None = None) -> dict[str, object]:
    return {
        "name": "环境节点",
        "description": "验证节点绑定不可变终端环境",
        "environment_version_id": environment_version_id,
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


def test_sensitive_path_scan_ignores_deleted_files_and_normalizes_results():
    assert _sensitive_paths(
        [
            "D /root/.bash_history",
            "D /root/.config/lark/token.json",
            "C /root/.ssh",
            "A /root/.ssh/id_ed25519",
            "A /root/.config/feishu/session.json",
            "A /root/.local/share/lark-cli/master.key",
            "C /usr/local/bin/lark-cli",
            "unexpected",
        ]
    ) == [
        "/root/.config/feishu/session.json",
        "/root/.local/share/lark-cli/master.key",
        "/root/.ssh",
        "/root/.ssh/id_ed25519",
    ]


def test_publish_inspects_commands_before_final_cleanup(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(environment_docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        environment_docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker", terminal_environment_publish_timeout_seconds=60
        ),
    )
    monkeypatch.setattr(
        environment_docker,
        "_inspect_commands",
        lambda container_id: calls.append("inspect") or {"agent-server": "1.0"},
    )
    monkeypatch.setattr(
        environment_docker,
        "_clean_ephemeral_files",
        lambda container_id: calls.append("clean"),
    )
    monkeypatch.setattr(
        environment_docker,
        "container_diff",
        lambda container_id: calls.append("scan") or [],
    )

    def fake_run(command, **kwargs):
        if "commit" in command:
            calls.append("commit")
            return "sha256:image"
        if command[-2:] == ["-c", "command -v agent-server"]:
            return "/runtime/bin/agent-server"
        if command[1:3] == ["image", "inspect"]:
            return '{"Id":"sha256:image","Architecture":"arm64","Os":"linux"}'
        raise AssertionError(command)

    monkeypatch.setattr(environment_docker, "_run", fake_run)
    published = environment_docker.publish_container(
        "container-1", environment_id="environment-1", version_no=1
    )

    assert calls == ["inspect", "clean", "scan", "commit"]
    assert published.reference == "flowweave/environment-environment-1:v1"


def test_terminal_environment_publish_and_node_binding(client, monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.start_setup_container",
        lambda image, environment_id: "setup-container-1",
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_container",
        lambda container_id, *, environment_id, version_no: PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + "a" * 64,
            manifest={
                "schema_version": 1,
                "commands": {"python": "Python 3.13", "lark-cli": "1.0.84"},
            },
        ),
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.remove_container",
        removed.append,
    )

    created = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "飞书工具环境",
            "description": "安装交互式 CLI",
            "base_image": "flowweave-openhands-runtime:1",
        },
    )
    assert created.status_code == 201, created.text
    environment = created.json()
    assert environment["versions"] == []

    setup = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={},
    )
    assert setup.status_code == 201, setup.text
    session = setup.json()
    assert session["state"] == "RUNNING"

    published = client.post(f"/api/v1/environment-setup-sessions/{session['id']}/publish")
    assert published.status_code == 201, published.text
    version = published.json()
    assert version["state"] == "READY"
    assert version["image_digest"] == "sha256:" + "a" * 64
    assert version["manifest"]["commands"]["lark-cli"] == "1.0.84"
    assert removed == ["setup-container-1"]

    retried = client.post(f"/api/v1/environment-setup-sessions/{session['id']}/publish")
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] == version["id"]
    assert removed == ["setup-container-1"]

    node = client.post("/api/v1/node-assets", json=_node_payload(version["id"]))
    assert node.status_code == 201, node.text
    asset = node.json()
    assert asset["environment_version_id"] == version["id"]
    assert asset["environment_version"]["image_digest"] == version["image_digest"]

    listed_version = client.get(f"/api/v1/terminal-environments/{environment['id']}").json()[
        "versions"
    ][0]
    assert listed_version["node_reference_count"] == 1
    assert listed_version["run_reference_count"] == 0
    assert listed_version["reference_count"] == 1

    blocked_version = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{version['id']}"
    )
    assert blocked_version.status_code == 409, blocked_version.text
    error = blocked_version.json()["error"]
    assert error["code"] == "ENVIRONMENT_VERSION_IN_USE"
    assert error["message"] == "The terminal environment version is still referenced"
    assert error["details"] == {
        "environment_id": environment["id"],
        "version_id": version["id"],
        "node_reference_count": 1,
        "run_reference_count": 0,
    }
    assert error["request_id"]

    blocked = client.delete(f"/api/v1/terminal-environments/{environment['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ENVIRONMENT_IN_USE"


def test_environment_version_run_reference_is_reported_and_blocks_deletion(
    client, db_session_factory
):
    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "运行占用环境",
            "description": "",
            "base_image": "flowweave-openhands-runtime:1",
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
    assert listed_version["node_reference_count"] == 0
    assert listed_version["run_reference_count"] == 1
    assert listed_version["reference_count"] == 1

    blocked = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{version_id}"
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ENVIRONMENT_VERSION_IN_USE"
    assert blocked.json()["error"]["details"]["run_reference_count"] == 1


def test_delete_unused_versions_clears_provenance_and_preserves_version_high_watermark(
    client, db_session_factory, monkeypatch
):
    containers: list[str] = []
    removed_images: list[str] = []

    def start_container(_image: str, _environment_id: str) -> str:
        container_id = f"setup-{len(containers) + 1}"
        containers.append(container_id)
        return container_id

    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.start_setup_container",
        start_container,
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.publish_container",
        lambda container_id, *, environment_id, version_no: PublishedImage(
            reference=f"flowweave/environment-{environment_id}:v{version_no}",
            digest="sha256:" + str(version_no) * 64,
            manifest={"schema_version": 1, "container_id": container_id},
        ),
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.remove_runtime_container",
        lambda _container_id: None,
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.remove_image",
        removed_images.append,
    )

    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "版本清理环境",
            "description": "",
            "base_image": "flowweave-openhands-runtime:1",
        },
    ).json()

    first_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions", json={}
    ).json()
    first = client.post(f"/api/v1/environment-setup-sessions/{first_session['id']}/publish").json()
    second_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions",
        json={"base_version_id": first["id"]},
    ).json()
    second = client.post(
        f"/api/v1/environment-setup-sessions/{second_session['id']}/publish"
    ).json()
    assert (first["version_no"], second["version_no"]) == (1, 2)

    deleted = client.delete(
        f"/api/v1/terminal-environments/{environment['id']}/versions/{first['id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert removed_images == [first["image_reference"]]
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
    third_session = client.post(
        f"/api/v1/terminal-environments/{environment['id']}/setup-sessions", json={}
    ).json()
    third = client.post(f"/api/v1/environment-setup-sessions/{third_session['id']}/publish").json()
    assert third["version_no"] == 3


def test_node_rejects_unknown_or_unready_environment_version(client):
    response = client.post(
        "/api/v1/node-assets",
        json=_node_payload("00000000-0000-0000-0000-000000000000"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ENVIRONMENT_VERSION_INVALID"


def test_setup_session_reports_disabled_backend(client):
    created = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "关闭后端环境",
            "description": "",
            "base_image": "flowweave-openhands-runtime:1",
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


def test_terminal_opens_bash(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    commands: list[list[str]] = []
    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker"))
    monkeypatch.setattr(docker.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(docker.os, "close", lambda _fd: None)

    process = object()

    def popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(docker.subprocess, "Popen", popen)

    master, opened = docker.open_terminal("setup-container-1")

    assert master == 10
    assert opened is process
    assert commands == [
        [
            "docker",
            "exec",
            "-it",
            "-e",
            "TERM=xterm-256color",
            "setup-container-1",
            "bash",
        ]
    ]


def test_terminal_can_attach_to_persistent_tmux_session(monkeypatch):
    from flowweave.modules.environments.infrastructure import docker

    commands: list[list[str]] = []
    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker"))
    monkeypatch.setattr(docker.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(docker.os, "close", lambda _fd: None)
    process = object()

    def popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(docker.subprocess, "Popen", popen)

    master, opened = docker.open_terminal(
        "runtime-container-1", session_name="FlowWeave/conversation:123"
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
            "tmux",
            "new-session",
            "-A",
            "-s",
            "flowweave-conversation-123",
            "bash",
        ]
    ]


def test_expired_setup_session_is_reclaimed_before_starting_another(
    client, db_session_factory, monkeypatch
):
    removed: list[str] = []
    started: list[str] = []

    def start_container(image: str, environment_id: str) -> str:
        del image, environment_id
        container_id = f"setup-container-{len(started) + 1}"
        started.append(container_id)
        return container_id

    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.start_setup_container",
        start_container,
    )
    monkeypatch.setattr(
        "flowweave.modules.environments.infrastructure.docker.remove_container",
        removed.append,
    )

    environment = client.post(
        "/api/v1/terminal-environments",
        json={
            "name": "过期回收环境",
            "description": "验证过期容器不会阻塞后续配置",
            "base_image": "flowweave-openhands-runtime:1",
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
    assert removed == ["setup-container-1"]

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
