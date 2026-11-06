"""Child registration follows the governed catalog and the target native lifecycle."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_child_sessions as child_fixtures
from sqlalchemy import select
from test_child_sessions import (  # noqa: F401
    gateway_base_url,
    launcher_http,
    parent_graph,
    parent_profile,
)

from flowweave.modules.agent_sessions.application import child_capabilities
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationCapability
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeMCPProbeResult
from flowweave.runtime.workspace import agent_workspace_capability_marketplace_name
from flowweave.shared.errors import DomainError

child_http = child_fixtures.child_http


def publish(client, kind, name, content="first"):
    if kind == "MCP":
        data = json.dumps({"mcpServers": {name: {"url": "https://mcp.example.test/mcp"}}}).encode()
        filename = name + ".json"
    elif kind == "CONTEXT":
        data, filename = content.encode(), name + ".md"
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            if kind == "SKILL":
                archive.writestr(
                    f"{name}/SKILL.md",
                    f"---\nname: {name}\ndescription: {content}\n---\n{content}\n",
                )
            else:
                archive.writestr(
                    ".plugin/plugin.json", json.dumps({"name": name, "version": "1.0.0"})
                )
                archive.writestr("skills/example/SKILL.md", "# Plugin skill\n")
        data, filename = buffer.getvalue(), name + ".zip"
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": kind,
            "filename": filename,
            "content_base64": base64.b64encode(data).decode(),
        },
    )
    assert response.status_code == 200, response.text
    committed = client.post(
        "/api/v1/capability-imports", json={"import_token": response.json()["import_token"]}
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]["capability_id"]


@pytest.fixture
def capabilities(child_http, container, tmp_path, monkeypatch):
    x = child_http
    from flowweave.shared.http import get_db

    async def get_test_db():
        async with x.client.app.state.container.database.session() as db:
            yield db

    x.client.app.dependency_overrides[get_db] = get_test_db
    x.client.app.state.container.artifact_store = container.artifact_store
    root = Path(x.g.config.workspace_root) / "capability-tests" / tmp_path.name / "parent"
    paths = (
        root,
        root,
        agent_workspace_capability_marketplace_name(x.loc.parent_id),
    )
    monkeypatch.setattr(child_capabilities, "marketplace_paths", lambda db, loc: paths)
    configuration = x.runtime.child_conversation_configuration

    def child_configuration(handle):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        original = configuration(handle)
        return original.model_copy(
            update={
                "agent": {
                    **original.agent,
                    "agent_context": {
                        "registered_marketplaces": [
                            {
                                "name": paths[2],
                                "source": str(paths[1] / "marketplace"),
                                "auto_load": False,
                            }
                        ]
                    },
                }
            }
        )

    x.runtime.child_conversation_configuration = child_configuration
    calls = []
    failing = set()

    def load(handle, plugin):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        assert handle.conversation_id == x.loc.handle.conversation_id
        if any(value in plugin for value in failing):
            raise DomainError("RUNTIME_PLUGIN_FAILED", "加载失败", 409)
        calls.append(plugin)

    x.runtime.load_plugin = load
    return SimpleNamespace(x=x, paths=paths, calls=calls, failing=failing)


def register(x, ids):
    return x.client.post(x.root + "/capabilities/batch", json={"capability_version_ids": ids})


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_capability_batch_partial_retry_and_skill_upgrade(capabilities):
    c, x = capabilities, capabilities.x
    first = publish(x.client, "SKILL", "child-skill")
    mcp = publish(x.client, "MCP", "child-mcp")
    plugin = publish(x.client, "PLUGIN", "child-plugin")
    c.failing.add(mcp)
    response = register(x, [first, mcp, plugin])
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["completed"] is False
    assert value["loaded_capability_version_ids"] == [first]
    assert value["failure"]["capability_version_id"] == mcp
    assert [entry["id"] for entry in value["conversation"]["capabilities"]] == [first]
    c.failing.clear()
    retried = register(x, [first, mcp, plugin])
    assert retried.status_code == 200, retried.text
    assert retried.json()["completed"] is True
    assert retried.json()["already_loaded_capability_version_ids"] == [first]
    assert retried.json()["loaded_capability_version_ids"] == [mcp, plugin]
    assert len(c.calls) == 3
    assert all(ref.startswith(f"child-{x.loc.binding_id}-") for ref in c.calls)
    assert register(x, [first, mcp, plugin]).json()["loaded_capability_version_ids"] == []
    upgraded = publish(x.client, "SKILL", "child-skill", "second")
    changed = register(x, [upgraded])
    assert changed.status_code == 200, changed.text
    ids = [entry["id"] for entry in changed.json()["conversation"]["capabilities"]]
    assert upgraded in ids and first not in ids
    assert register(x, [first]).status_code == 409
    with x.g.sessions() as db:
        parent_refs = list(
            db.scalars(
                select(AgentConversationCapability).where(
                    AgentConversationCapability.binding_id == x.loc.parent_id,
                )
            )
        )
        assert parent_refs == []
    manifest = json.loads((c.paths[0] / "marketplace/.plugin/marketplace.json").read_text())
    assert len(manifest["plugins"]) == 4
    assert f"child-{x.loc.binding_id}" in manifest["plugins"][2]["source"]


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_capability_validation_busy_permission_and_guard(capabilities):
    c, x = capabilities, capabilities.x
    skill = publish(x.client, "SKILL", "child-skill")
    context = publish(x.client, "CONTEXT", "child-context")
    assert register(x, [skill, skill]).status_code == 422
    assert register(x, [skill, context]).status_code == 422
    assert not c.calls
    x.runtime.can_accept_input = lambda handle: False
    assert register(x, [skill]).status_code == 409
    x.runtime.can_accept_input = lambda handle: True
    x.client.cookies.set("flowweave_session", "other-cookie")
    with tenant_user(x.g.other):
        assert register(x, [skill]).status_code in {403, 404}
    x.client.cookies.set("flowweave_session", "owner-cookie")
    with child_capabilities.registration_guard(x.loc.parent_id, x.loc.binding_id):
        assert register(x, [skill]).status_code == 409
    assert not c.calls
    configuration = x.runtime.child_conversation_configuration
    x.runtime.child_conversation_configuration = lambda handle: configuration(handle).model_copy(
        update={"agent": {}}
    )
    assert (
        register(x, [skill]).json()["error"]["code"] == "AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE"
    )
    assert not c.calls


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_mcp_probe_targets_its_host_and_keeps_secrets_request_only(capabilities, monkeypatch):
    c, x = capabilities, capabilities.x
    mcp = publish(x.client, "MCP", "child-mcp")
    secret = "private-probe-bearer"
    monkeypatch.setattr(
        child_capabilities.mcp_authentication,
        "prepare_server",
        lambda db, server, **kwargs: (
            replace(
                server, config={**server.config, "auth": {"strategy": "bearer", "value": secret}}
            ),
            {},
        ),
    )
    probes = []

    def probe(request):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        assert request.runtime_resource_name == x.loc.handle.runtime_resource_name
        assert request.server.config["auth"]["value"] == secret
        for path in c.paths[0].rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
        probes.append(request)
        return RuntimeMCPProbeResult(ok=True, tools=("read",))

    x.runtime.probe_mcp = probe
    response = x.client.post(x.root + f"/capabilities/{mcp}/mcp-readiness", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "READY", response.text
    assert response.json()["tools"] == ["read"]
    assert secret not in response.text and len(probes) == 1
    assert not list(c.paths[0].rglob("*.json"))
    assert not c.calls


def test_shared_marketplace_preserves_simultaneous_parent_and_child_publications(
    settings, tmp_path
):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from threading import Barrier
    from uuid import uuid4

    from flowweave.runtime.workspace import materialize_agent_workspace_capability_marketplace
    from flowweave.shared.settings import settings_context

    root = Path(settings.workspace_root) / "concurrent-marketplace" / tmp_path.name
    barrier = Barrier(8)

    def materialize(index):
        barrier.wait(timeout=10)
        return materialize_agent_workspace_capability_marketplace(
            {
                "capability_type": "MCP",
                "capability_key": f"server-{index}",
                "capability_version_id": str(uuid4()),
                "digest": str(index) * 64,
                "normalized_config": {"url": f"https://mcp.example.test/{index}"},
            },
            host_root=root,
            runtime_root=Path("/runtime/capabilities/shared"),
            marketplace_name="shared",
            plugin_namespace=f"child-{index}" if index else None,
        )

    with settings_context(settings), ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(copy_context().run, materialize, index) for index in range(8)]
        names = [future.result(timeout=20) for future in futures]
    manifest = json.loads((root / "marketplace/.plugin/marketplace.json").read_text())
    assert {entry["name"] for entry in manifest["plugins"]} == set(names)
    assert len(manifest["plugins"]) == 8
