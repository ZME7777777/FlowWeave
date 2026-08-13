from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

import pytest

from flowweave.shared.application.plugin_resolver import PluginResolveRequest
from flowweave.shared.infrastructure import plugin_resolver as resolver_module
from flowweave.shared.infrastructure.plugin_resolver import (
    DockerPluginResolver,
    validate_plugin_git_source,
)

COMMIT = "a" * 40
HOSTS = frozenset({"github.com", "gitlab.com"})


def _resolver() -> DockerPluginResolver:
    return DockerPluginResolver(
        "flowweave-openhands-runtime:locked",
        allowed_hosts=HOSTS,
        manager_scope="test-scope",
        timeout_seconds=30,
        cleanup_grace_seconds=60,
    )


@pytest.mark.parametrize(
    "resolve_request",
    (
        PluginResolveRequest("http://github.com/acme/plugin", COMMIT),
        PluginResolveRequest("https://user@github.com/acme/plugin", COMMIT),
        PluginResolveRequest("https://github.com:444/acme/plugin", COMMIT),
        PluginResolveRequest("https://github.com/acme/plugin?ref=main", COMMIT),
        PluginResolveRequest("https://github.com/acme/%2e%2e/plugin", COMMIT),
        PluginResolveRequest("https://github.com/acme//plugin", COMMIT),
        PluginResolveRequest("https://example.com/acme/plugin", COMMIT),
        PluginResolveRequest("https://github.com/acme/plugin", "main"),
        PluginResolveRequest("https://github.com/acme/plugin", COMMIT, "../plugin"),
    ),
)
def test_git_plugin_source_requires_public_https_allowlist_and_full_commit(
    resolve_request: PluginResolveRequest,
) -> None:
    with pytest.raises(ValueError):
        validate_plugin_git_source(resolve_request, HOSTS)


def test_git_plugin_source_normalizes_commit_and_accepts_safe_subpath() -> None:
    request = validate_plugin_git_source(
        PluginResolveRequest("https://github.com/acme/plugins.git", "A" * 40, "plugins/review"),
        HOSTS,
    )

    assert request == PluginResolveRequest(
        "https://github.com/acme/plugins.git", COMMIT, "plugins/review"
    )

    canonical = validate_plugin_git_source(
        PluginResolveRequest("https://GITHUB.COM:443/acme/plugins.git/", COMMIT),
        HOSTS,
    )
    assert canonical.source == "https://github.com/acme/plugins.git"


def test_plugin_resolution_uses_owned_ephemeral_network_and_fixed_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    removed: list[tuple[str, str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["network", "create"]:
            return subprocess.CompletedProcess(command, 0, stdout="network-id", stderr="")
        response = {
            "content_base64": base64.b64encode(b"plugin-zip").decode(),
            "resolved_commit": COMMIT,
            "report": {"schema_version": 1, "openhands_version": "1.40.0"},
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(response), stderr="")

    monkeypatch.setattr(resolver_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        resolver_module,
        "remove_owned_container",
        lambda _docker, name, resource_id, **_kwargs: (removed.append((name, resource_id)) or True),
    )
    monkeypatch.setattr(
        resolver_module,
        "remove_owned_network",
        lambda _docker, name, resource_id, **_kwargs: (removed.append((name, resource_id)) or True),
    )

    bundle = _resolver().resolve(
        PluginResolveRequest("https://github.com/acme/plugins.git", COMMIT)
    )

    assert bundle.content == b"plugin-zip"
    network, container = commands
    assert "flowweave.network-purpose=plugin-resolve" in network
    assert "flowweave.network-mode=egress" in network
    assert container[container.index("--network") + 1] == network[-1]
    assert container[container.index("--entrypoint") + 1] == "/runtime/.venv/bin/python"
    assert container[-2:] == [
        "-I",
        "/runtime/plugin_resolver.py",
    ]
    assert "--privileged" not in container
    assert container[container.index("--user") + 1] == "65534:65534"
    assert container[container.index("--memory") + 1] == "512m"
    assert removed[0][1] == removed[1][1]


def test_plugin_resolution_rejects_commit_drift_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[str] = []
    calls = 0

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, stdout="network-id", stderr="")
        response = {
            "content_base64": base64.b64encode(b"plugin-zip").decode(),
            "resolved_commit": "b" * 40,
            "report": {},
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(response), stderr="")

    monkeypatch.setattr(resolver_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        resolver_module,
        "remove_owned_container",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or True,
    )
    monkeypatch.setattr(
        resolver_module,
        "remove_owned_network",
        lambda _docker, name, _resource_id, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(RuntimeError, match="invalid bundle"):
        _resolver().resolve(PluginResolveRequest("https://github.com/acme/plugins.git", COMMIT))

    assert len(removed) == 2
