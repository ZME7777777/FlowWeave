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


def test_flatten_setup_container_pauses_exports_imports_and_unpauses(monkeypatch) -> None:
    commands: list[list[str]] = []

    class Process:
        def __init__(self, stdout: bytes = b"", *, returncode: int = 0) -> None:
            self.stdout = io.BytesIO(stdout)
            self.returncode = returncode

        def communicate(self, timeout=None):
            assert timeout == 60
            return b"sha256:" + b"c" * 64, b""

        def wait(self, timeout=None):
            assert timeout == 30
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            pytest.fail("successful flatten must not kill Docker processes")

    exporter = Process(b"tar-stream")
    importer = Process()
    popens: list[tuple[list[str], object]] = []

    def fake_popen(command, **kwargs):
        popens.append((command, kwargs.get("stdin")))
        return exporter if command[1] == "export" else importer

    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker, "_run", lambda command, **_kwargs: commands.append(command) or ""
    )
    monkeypatch.setattr(environment_docker.subprocess, "Popen", fake_popen)

    assert environment_docker._flatten_setup_container(
        "setup-container", "flowweave/environment-base:test", timeout=60
    ) == "sha256:" + "c" * 64
    assert commands == [
        ["docker", "pause", "setup-container"],
        ["docker", "unpause", "setup-container"],
    ]
    assert popens[0][0] == ["docker", "export", "setup-container"]
    assert popens[1][0] == [
        "docker",
        "import",
        "--change",
        "ENTRYPOINT []",
        "--change",
        "USER 0:0",
        "-",
        "flowweave/environment-base:test",
    ]
    assert popens[1][1] is exporter.stdout


def test_flatten_setup_container_unpauses_after_import_failure(monkeypatch) -> None:
    class Process:
        def __init__(self, *, returncode: int, stderr: bytes = b"") -> None:
            self.stdout = io.BytesIO(b"tar-stream")
            self.returncode = returncode
            self.stderr = stderr

        def communicate(self, timeout=None):
            assert timeout == 60
            return b"", self.stderr

        def wait(self, timeout=None):
            assert timeout == 30
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            pytest.fail("completed processes must not be killed")

    exporter = Process(returncode=0)
    importer = Process(returncode=1, stderr=b"docker import failed")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        environment_docker, "get_settings", lambda: SimpleNamespace(docker_binary="docker")
    )
    monkeypatch.setattr(
        environment_docker, "_run", lambda command, **_kwargs: commands.append(command) or ""
    )
    monkeypatch.setattr(
        environment_docker.subprocess,
        "Popen",
        lambda command, **_kwargs: exporter if command[1] == "export" else importer,
    )

    with pytest.raises(DomainError, match="ENVIRONMENT_DOCKER_FAILED"):
        environment_docker._flatten_setup_container(
            "setup-container", "flowweave/environment-base:test", timeout=60
        )

    assert commands == [
        ["docker", "pause", "setup-container"],
        ["docker", "unpause", "setup-container"],
    ]
