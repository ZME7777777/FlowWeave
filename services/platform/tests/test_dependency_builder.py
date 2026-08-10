from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

import pytest

from flowweave.shared.infrastructure import dependency_builder as builder_module
from flowweave.shared.infrastructure.dependency_builder import DockerDependencyBuilder


def _builder() -> DockerDependencyBuilder:
    return DockerDependencyBuilder(
        "flowweave-dependency-builder:locked",
        manager_scope="test-scope",
        timeout_seconds=30,
        cleanup_grace_seconds=60,
    )


def test_dependency_build_uses_owned_ephemeral_network_and_bounded_logs(monkeypatch) -> None:
    commands: list[list[str]] = []
    removed: list[tuple[str, str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["network", "create"]:
            return subprocess.CompletedProcess(command, 0, stdout="network-id", stderr="")
        payload = {
            "content_base64": base64.b64encode(b"bundle").decode(),
            "manifest": {"schema_version": 1},
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder_module,
        "remove_owned_container",
        lambda _docker, name, resource_id, **_kwargs: removed.append((name, resource_id)) or True,
    )
    monkeypatch.setattr(
        builder_module,
        "remove_owned_network",
        lambda _docker, name, resource_id, **_kwargs: removed.append((name, resource_id)) or True,
    )

    result = _builder().build({"python": {"httpx": "0.28.1"}})

    assert result.content == b"bundle"
    network, container = commands
    assert network[:4] == ["docker", "network", "create", "--driver"]
    network_labels = [network[index + 1] for index, item in enumerate(network) if item == "--label"]
    assert "flowweave.resource-type=network" in network_labels
    assert "flowweave.network-purpose=dependency-build" in network_labels
    assert "flowweave.network-mode=egress" in network_labels
    assert container[container.index("--network") + 1] == network[-1]
    assert container[container.index("--log-driver") + 1] == "local"
    assert container[container.index("--storage-opt") + 1] == "size=4g"
    assert [
        container[index + 1] for index, item in enumerate(container) if item == "--log-opt"
    ] == [
        "max-size=4m",
        "max-file=2",
    ]
    assert removed[0][0] == container[container.index("--name") + 1]
    assert removed[1][0] == network[-1]
    assert removed[0][1] == removed[1][1]


def test_dependency_build_cleans_up_after_timeout(monkeypatch) -> None:
    removed: list[str] = []
    calls = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, stdout="network-id", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder_module,
        "remove_owned_container",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or True,
    )
    monkeypatch.setattr(
        builder_module,
        "remove_owned_network",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        _builder().build({"node": {"typescript": "5.9.2"}})

    assert len(removed) == 2
    assert removed[0].startswith("fw-ep-dependencybuild-")
    assert removed[1].startswith("fw-net-")


def test_dependency_network_creation_failure_still_runs_safe_cleanup(monkeypatch) -> None:
    removed: list[str] = []
    monkeypatch.setattr(
        builder_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="network create failed"
        ),
    )
    monkeypatch.setattr(
        builder_module,
        "remove_owned_container",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or False,
    )
    monkeypatch.setattr(
        builder_module,
        "remove_owned_network",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or False,
    )

    with pytest.raises(RuntimeError, match="network failed"):
        _builder().build({"cli": {"example": "1.2.3"}})

    assert len(removed) == 2
