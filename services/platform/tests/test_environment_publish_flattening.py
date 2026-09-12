from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from flowweave.modules.environments.infrastructure import docker as environment_docker
from flowweave.shared.errors import DomainError


def test_ghcr_tls_timeout_has_stable_safe_diagnostic() -> None:
    error = environment_docker._docker_command_failed(
        'Head "https://ghcr.io/v2/astral-sh/uv/manifests/0.11.6": '
        "net/http: TLS handshake timeout"
    )

    assert error.code == "ENVIRONMENT_BUILD_REGISTRY_UNAVAILABLE"
    assert error.status == 503
    assert error.details == {"registry": "ghcr.io", "failure": "TLS_HANDSHAKE_TIMEOUT"}


def test_flatten_setup_container_only_pauses_live_setup(monkeypatch) -> None:
    commands: list[list[str]] = []
    exports: list[tuple[str, str, int]] = []
    normalized: list[tuple[str, int]] = []

    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker, "_run", lambda command, **_kwargs: commands.append(command) or ""
    )
    monkeypatch.setattr(
        environment_docker,
        "_export_container_as_single_layer",
        lambda container_id, reference, *, timeout: exports.append(
            (container_id, reference, timeout)
        )
        or "sha256:initial",
    )
    monkeypatch.setattr(
        environment_docker,
        "_normalize_imported_debian_sources",
        lambda reference, *, timeout: normalized.append((reference, timeout))
        or "sha256:normalized",
    )

    assert environment_docker._flatten_setup_container(
        "setup-container", "flowweave/environment-base:test", timeout=60
    ) == "sha256:normalized"
    assert commands == [
        ["docker", "pause", "setup-container"],
        ["docker", "unpause", "setup-container"],
    ]
    assert exports == [("setup-container", "flowweave/environment-base:test", 60)]
    assert normalized == [("flowweave/environment-base:test", 60)]


def test_flatten_setup_container_unpauses_after_export_failure(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker, "_run", lambda command, **_kwargs: commands.append(command) or ""
    )
    monkeypatch.setattr(
        environment_docker,
        "_export_container_as_single_layer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DomainError("ENVIRONMENT_DOCKER_FAILED", "failed", 502)
        ),
    )

    with pytest.raises(DomainError, match="ENVIRONMENT_DOCKER_FAILED"):
        environment_docker._flatten_setup_container(
            "setup-container", "flowweave/environment-base:test", timeout=60
        )

    assert commands == [
        ["docker", "pause", "setup-container"],
        ["docker", "unpause", "setup-container"],
    ]


def test_normalize_imported_debian_sources_uses_only_temp_container(monkeypatch) -> None:
    commands: list[list[str]] = []
    exports: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker, "_run", lambda command, **_kwargs: commands.append(command) or ""
    )
    monkeypatch.setattr(
        environment_docker,
        "_export_container_as_single_layer",
        lambda container_id, reference, *, timeout: exports.append(
            (container_id, reference, timeout)
        )
        or "sha256:normalized",
    )

    assert environment_docker._normalize_imported_debian_sources(
        "flowweave/environment-base:test", timeout=60
    ) == "sha256:normalized"

    name = "fw-env-source-normalize-947ecbcdd77aa3ce0c926762"
    assert commands[0][:7] == [
        "docker",
        "create",
        "--name",
        name,
        "--entrypoint",
        "sh",
        "flowweave/environment-base:test",
    ]
    assert "mirrors.tuna.tsinghua.edu.cn/debian" in commands[0][-1]
    assert "deb.debian.org/debian" in commands[0][-1]
    assert "rm -rf -- /agent-server" in commands[0][-1]
    assert "/workspace" not in commands[0][-1]
    assert "/home/openhands" not in commands[0][-1]
    assert commands[1] == ["docker", "start", "-a", name]
    assert exports == [(name, "flowweave/environment-base:test", 60)]
    assert commands[2] == ["docker", "rm", "--force", name]


def test_export_container_as_single_layer_sets_root_entrypoint(monkeypatch) -> None:
    class Process:
        def __init__(self, stdout: bytes = b"", stderr: bytes = b"") -> None:
            self.stdout = io.BytesIO(stdout)
            self.stderr = stderr
            self.returncode = 0

        def communicate(self, timeout=None):
            assert timeout == 60
            return b"sha256:flattened\n", self.stderr

        def wait(self, timeout=None):
            assert timeout == 30
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            pytest.fail("successful export/import must not kill Docker processes")

    exporter = Process(b"tar-stream")
    importer = Process()
    calls: list[tuple[list[str], object]] = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs.get("stdin")))
        return exporter if command[1] == "export" else importer

    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(environment_docker.subprocess, "Popen", fake_popen)

    assert environment_docker._export_container_as_single_layer(
        "temporary-container", "flowweave/environment-base:test", timeout=60
    ) == "sha256:flattened"
    assert calls[0][0] == ["docker", "export", "temporary-container"]
    assert calls[1][0] == [
        "docker",
        "import",
        "--change",
        "ENTRYPOINT []",
        "--change",
        "USER 0:0",
        "-",
        "flowweave/environment-base:test",
    ]
    assert calls[1][1] is exporter.stdout


def test_runtime_probe_inherits_the_frozen_openhands_build_identity(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["run", "--detach"]:
            return "probe-container"
        if command[1] == "rm":
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(environment_docker, "_run", fake_run)
    monkeypatch.setattr(
        environment_docker, "_inspect_commands", lambda _container_id: {"python": "Python"}
    )
    monkeypatch.setattr(
        environment_docker,
        "_inspect_runtime_provenance",
        lambda _container_id: {"source_commit": "30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9"},
    )
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: commands.append(command)
        or ("contract passed" if command[1] == "exec" else "probe-container"),
    )

    environment_docker._probe_runtime_image("sha256:image", probe_token="version-token")

    probe_start = commands[0]
    assert probe_start[0:8] == [
        "docker",
        "run",
        "--detach",
        "--name",
        "fw-env-probe-version-token",
        "--entrypoint",
        "sh",
        "--tmpfs",
    ]
    assert probe_start[8] == "/runtime/state:uid=10001,gid=10001,mode=0700"
    assert probe_start[9:11] == [
        "--env",
        "OPENHANDS_BUILD_GIT_SHA=30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9",
    ]
    assert probe_start[11:13] == [
        "--env",
        "OPENHANDS_BUILD_GIT_REF=30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9",
    ]
    assert commands[1] == [
        "docker",
        "cp",
        "/app/contract_check.py",
        "fw-env-probe-version-token:/tmp/flowweave-contract-check.py",
    ]
    contract_check = commands[2]
    assert contract_check[0:8] == [
        "docker",
        "exec",
        "--env",
        "HOME=/tmp",
        "--env",
        "OPENHANDS_BUILD_GIT_SHA=30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9",
        "--env",
        "OPENHANDS_BUILD_GIT_REF=30cf5832e42c71c24daa82a1a4fd5d25eb70d1b9",
    ]
    assert contract_check[-1] == "/tmp/flowweave-contract-check.py"


def test_runtime_provenance_stamp_uses_only_disposable_governance_container(monkeypatch) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker,
        "_run",
        lambda command, **_kwargs: commands.append(command) or "sha256:stamped",
    )

    assert environment_docker._stamp_fixed_runtime_provenance(
        "sha256:formal", "flowweave/environment:test", timeout=60
    ) == "sha256:stamped"

    name = "fw-env-provenance-" + __import__("hashlib").sha256(
        b"flowweave/environment:test"
    ).hexdigest()[:24]
    assert commands[0] == [
        "docker",
        "create",
        "--name",
        name,
        "sha256:formal",
    ]
    assert commands[1] == [
        "docker",
        "cp",
        "/app/openhands-source-provenance.json",
        f"{name}:/runtime/openhands-source-provenance.json",
    ]
    assert commands[2] == [
        "docker",
        "cp",
        "/app/patch_fork_condenser.py",
        f"{name}:/runtime/patch_fork_condenser.py",
    ]
    assert commands[3] == [
        "docker",
        "commit",
        name,
        "flowweave/environment:test",
    ]
    assert commands[4] == ["docker", "rm", "--force", name]
