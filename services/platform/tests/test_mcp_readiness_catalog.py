"""Catalog probes use the user Runtime and never gain permission to read history."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import (
    conversations,
    history_integration,
    history_probe,
)
from flowweave.runtime.base import RuntimeMCP, RuntimeMCPProbeResult
from flowweave.shared.settings import settings_context


@pytest.fixture(autouse=True)
def database():
    """No external database; native I/O is controlled at the existing probe boundary."""


@pytest.fixture
def catalog_probe(monkeypatch, tmp_path):
    owner, version_id = str(uuid4()), str(uuid4())
    published = SimpleNamespace(
        package=SimpleNamespace(
            capability_type="MCP", capability_key=history_integration.SERVER_NAME
        ),
        version=SimpleNamespace(id=version_id),
    )
    monkeypatch.setattr(conversations, "_workspace", lambda *_: SimpleNamespace(id="workspace"))
    monkeypatch.setattr(conversations, "resolve_version", lambda *_: published)
    monkeypatch.setattr(history_integration, "configured_capability", lambda *_: published)
    monkeypatch.setattr(conversations, "current_user_id", lambda: owner)
    monkeypatch.setattr(
        conversations, "get_settings", lambda: SimpleNamespace(workspace_root=tmp_path)
    )
    monkeypatch.setattr(
        conversations.agent_workspace_host,
        "runtime_allocation_for_agent_workspace",
        lambda *_: SimpleNamespace(relative_root="owned"),
    )
    monkeypatch.setattr(conversations, "_frozen_runtime_capability", lambda *_: {})
    requests = []

    def materialize(_capabilities, *, host_root, runtime_root):
        host_root.mkdir(parents=True)
        (host_root / "probe.json").write_text("{}")
        return (
            (),
            (),
            (
                RuntimeMCP(
                    name=history_integration.SERVER_NAME,
                    config={"transport": "streamable-http", "url": "https://example.test/mcp"},
                ),
            ),
        )

    monkeypatch.setattr(conversations, "materialize_agent_workspace_capabilities", materialize)
    result = RuntimeMCPProbeResult(ok=True, tools=("read_referenced_conversation",))

    def probe(request):
        requests.append(request)
        return result

    monkeypatch.setattr(conversations, "get_runtime", lambda: SimpleNamespace(probe_mcp=probe))

    def run(active=True):
        values = iter(
            [
                SimpleNamespace(status="ACTIVE" if active else "STOPPED", active_generation=1),
                SimpleNamespace(backend_resource_name="owned-runtime"),
            ]
        )
        with settings_context(Settings(_env_file=None)):
            return conversations.probe_workspace_mcp_readiness(
                SimpleNamespace(scalar=lambda _: next(values)), "workspace", version_id
            )

    return SimpleNamespace(
        run=run,
        requests=requests,
        root=tmp_path,
        published=published,
        owner=owner,
        version_id=version_id,
    )


def test_builtin_probe_returns_tools_with_catalog_only_authority(catalog_probe):
    g = catalog_probe
    result = g.run()
    assert result["state"] == "READY"
    assert result["tools"] == ["read_referenced_conversation"]
    assert len(g.requests) == 1
    request = g.requests[0]
    assert request.runtime_resource_name == "owned-runtime"
    assert request.read_only_tool_call is None
    token = request.server.config["headers"]["Authorization"][7:]
    with settings_context(Settings(_env_file=None)):
        assert history_probe.identity(token) == (g.owner, g.version_id)
    assert token not in str(result)
    assert not list(g.root.rglob("probe.json"))


def test_probe_does_not_start_stopped_runtime(catalog_probe):
    result = catalog_probe.run(active=False)
    assert result["reason"] == "RUNTIME_NOT_READY"
    assert result["tools"] == []
    assert not catalog_probe.requests


def test_builtin_probe_rejects_other_published_version(catalog_probe, monkeypatch):
    monkeypatch.setattr(
        history_integration,
        "configured_capability",
        lambda _: SimpleNamespace(version=SimpleNamespace(id=str(uuid4()))),
    )
    result = catalog_probe.run()
    assert result["reason"] == "AGENT_HISTORY_VERSION_NOT_CURRENT"
    assert not catalog_probe.requests
    assert not list(catalog_probe.root.rglob("probe.json"))


@pytest.mark.parametrize(
    "native_result, expected",
    [
        (RuntimeMCPProbeResult(ok=True, tools=("other_tool",)), "TOOLS_MISSING"),
        (RuntimeMCPProbeResult(ok=False, error_kind="connection"), "MCP_CONNECTION"),
        (RuntimeMCPProbeResult(ok=False, error_kind="timeout"), "MCP_TIMEOUT"),
    ],
)
def test_probe_failure_has_no_stale_tool_catalog(
    catalog_probe, monkeypatch, native_result, expected
):
    monkeypatch.setattr(
        conversations, "get_runtime", lambda: SimpleNamespace(probe_mcp=lambda _: native_result)
    )
    result = catalog_probe.run()
    assert result["state"] == "UNAVAILABLE"
    assert result["reason"] == expected
    assert result["tools"] == []
    assert not list(catalog_probe.root.rglob("probe.json"))
