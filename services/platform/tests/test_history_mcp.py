import asyncio
import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from flowweave.bootstrap.api import create_app
from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import conversations, history_access
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationHistoryGrant
from flowweave.modules.agent_sessions.presentation import history_mcp as mcp
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.modules.users.infrastructure.models import LdapAgentSessionOrganizationGrant, User
from flowweave.runtime.base import RuntimeEventBatch
from flowweave.shared.errors import DomainError
from flowweave.shared.secret_redaction import redact_secret_text, redact_secret_value


@pytest.fixture(autouse=True)
def database():
    """Exercise grant persistence in SQLite; no external database or Runtime."""


@pytest.fixture
def access(monkeypatch):
    owner = str(uuid4())
    target = SimpleNamespace(
        id=str(uuid4()),
        owner_user_id=owner,
        lifecycle="ACTIVE",
        openhands_conversation_id="native-target",
        runtime_session_id="runtime-target",
        host_kind="AGENT_WORKSPACE",
        host_id="workspace",
    )
    source = SimpleNamespace(
        id=str(uuid4()),
        owner_user_id=owner,
        lifecycle="ACTIVE",
        openhands_conversation_id="native-source",
        runtime_session_id="runtime-source",
        host_kind="AGENT_WORKSPACE",
        host_id="workspace",
    )
    bindings = {target.id: target, source.id: source}

    def workspace(db, workspace_id):
        assert workspace_id == "workspace"
        return SimpleNamespace(id=workspace_id)

    def binding(db, workspace_id, binding_id):
        item = bindings.get(binding_id)
        if item is None or item.owner_user_id != current_user_id():
            raise DomainError("AGENT_CONVERSATION_NOT_FOUND", "not found", 404)
        return item

    monkeypatch.setattr(conversations, "_workspace", workspace)
    monkeypatch.setattr(
        conversations,
        "history_reference_bindings",
        lambda db, workspace_id, target_id, source_id: (
            binding(db, workspace_id, target_id),
            binding(db, workspace_id, source_id),
        ),
    )
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def config(dbapi, _record):
        dbapi.create_function("set_config", 3, lambda *_args: "")

    AgentConversationHistoryGrant.__table__.create(engine)
    User.__table__.create(engine)
    LdapAgentSessionOrganizationGrant.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db, tenant_user(owner):
        user = User(id=owner, username="owner", role="SUPER_ADMIN", is_active=True)
        db.add(user)
        db.flush()
        grant, token = history_access.issue(db, "workspace", target.id, source.id)
        db.commit()
        yield SimpleNamespace(
            db=db,
            owner=owner,
            grant=grant,
            token=token,
            target=target,
            source=source,
            bindings=bindings,
            user=user,
        )
    engine.dispose()


def test_grant_stores_only_hash_and_revokes(access):
    a = access
    assert a.grant.token_digest == hashlib.sha256(a.token.encode()).hexdigest()
    assert a.token not in repr(a.grant.__dict__)
    assert a.token not in redact_secret_text(f"unexpected tool output: {a.token}")
    assert a.token not in str(redact_secret_value({"debug": {"content": a.token}}))
    assert history_access.authorize(a.db, a.token).id == a.grant.id
    with pytest.raises(DomainError):
        history_access.authorize(a.db, a.token, source_binding_id=a.source.id)
    history_access.revoke(a.db, "workspace", a.target.id, a.grant.id)
    with pytest.raises(DomainError) as error:
        history_access.authorize(a.db, a.token)
    assert error.value.status == 401


@pytest.mark.parametrize(
    "change",
    [
        "nonce",
        "owner",
        "grant",
        "expired",
        "deleted",
        "native",
        "runtime",
        "lifecycle",
        "disabled",
        "permission_revoked",
    ],
)
def test_invalid_or_drifted_authority_fails(access, change):
    a = access
    token = a.token
    if change == "nonce":
        token = token[:-1] + ("x" if token[-1] != "x" else "y")
    elif change == "owner":
        token = token.replace(a.owner, str(uuid4()))
    elif change == "grant":
        token = token.replace(a.grant.id, str(uuid4()))
    elif change == "expired":
        a.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "deleted":
        del a.bindings[a.source.id]
    elif change == "native":
        a.source.openhands_conversation_id = "replaced"
    elif change == "runtime":
        a.target.runtime_session_id = "replaced"
    elif change == "lifecycle":
        a.source.lifecycle = "DELETE_PENDING"
    elif change == "disabled":
        a.user.is_active = False
    else:
        a.user.role = "USER"
        a.user.agent_sessions_enabled = False
    a.db.flush()
    with pytest.raises(DomainError) as error:
        history_access.authorize(a.db, token)
    assert error.value.code == "AGENT_HISTORY_ACCESS_DENIED"


def test_foreign_owner_cannot_revoke_or_issue(access):
    with tenant_user(str(uuid4())), pytest.raises(DomainError):
        history_access.revoke(access.db, "workspace", access.target.id, access.grant.id)
    with tenant_user(str(uuid4())), pytest.raises(DomainError):
        history_access.issue(access.db, "workspace", access.target.id, access.source.id)


@pytest.mark.parametrize("token", ["", "x", "fwh1.bad.bad.nonce", "fwh1." + "x" * 500])
def test_malformed_tokens_fail(token):
    with pytest.raises(DomainError):
        history_access.token_identity(token)


@pytest.mark.parametrize("params", [{}, {"event_id": "formal-event", "content_offset": 1}])
def test_projection_rechecks_revocation_before_returning_body(access, monkeypatch, params):
    monkeypatch.setattr(conversations, "prepare_conversation_events", lambda *_args: object())
    monkeypatch.setattr(
        conversations, "assert_prepared_conversation_events_current", lambda *_args: None
    )
    args = mcp.HistoryArguments(**params)
    page = mcp.prepare_page(access.db, access.token, args)
    history_access.revoke(access.db, "workspace", access.target.id, access.grant.id)
    with pytest.raises(DomainError):
        mcp.project_page(access.db, page, RuntimeEventBatch(events=()), access.token, args)


@pytest.fixture
def client(monkeypatch):
    token = f"fwh1.{uuid4()}.{uuid4()}." + "a" * 43
    state = SimpleNamespace(authorized=True, reads=0, rate_allowed=True)

    def authorize(db, actual):
        if not state.authorized or actual != token:
            raise history_access.denied()
        return SimpleNamespace(owner_user_id="owner", id="grant")

    async def control(container, fn):
        return fn(None)

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(allowed=state.rate_allowed)

    async def read(*_args, **kwargs):
        assert kwargs["history"] is True
        state.reads += 1
        if getattr(state, "history_unavailable", False):
            raise DomainError("RUNTIME_READ_SATURATED", "busy", 503)
        return {"messages": [{"event_id": "formal", "content": "正文"}], "history_cursor": "older"}

    monkeypatch.setattr(history_access, "authorize", authorize)
    monkeypatch.setattr(
        mcp, "agent_session_principal", lambda *_args: SimpleNamespace(user_id="owner")
    )
    monkeypatch.setattr(mcp, "run_blocking_history", control)
    monkeypatch.setattr(mcp, "run_formal_events", read)
    app = FastAPI()
    app.state.container = SimpleNamespace(rate_limiter=SimpleNamespace(allow=allow))
    app.include_router(mcp.router)

    @app.exception_handler(DomainError)
    async def error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    with TestClient(app) as value:
        yield value, token, state


def test_mcp_handshake_tool_schema_and_read(client):
    c, token, state = client
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

    def rpc(method, params=None):
        return c.post(
            mcp.MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )

    initialized = rpc("initialize", {"protocolVersion": "2025-03-26"})
    assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
    tool = rpc("tools/list").json()["result"]["tools"][0]
    assert tool["annotations"]["readOnlyHint"] is True
    properties = tool["inputSchema"]["properties"]
    assert "source_binding_id" in properties
    assert "36-character" in properties["source_binding_id"]["description"]
    assert "1-10000" in properties["max_content_chars"]["description"]
    result = rpc("tools/call", {"name": tool["name"], "arguments": {}})
    assert result.headers["cache-control"] == "no-store"
    assert json.loads(result.json()["result"]["content"][0]["text"])["history_cursor"] == "older"
    assert state.reads == 1
    capped = rpc("tools/call", {"name": tool["name"], "arguments": {"max_content_chars": 12_000}})
    assert "result" in capped.json()
    assert state.reads == 2
    invalid = rpc("tools/call", {"name": tool["name"], "arguments": {"source_binding_id": "other"}})
    invalid_result = invalid.json()["result"]
    assert invalid_result["isError"] is True
    invalid_detail = json.loads(invalid_result["content"][0]["text"])
    assert invalid_detail["error"] == "invalid_arguments"
    assert "source_binding_id" in invalid_detail["guidance"]
    assert state.reads == 2
    state.authorized = False
    assert rpc("tools/list").status_code == 401
    assert state.reads == 2


def test_mcp_history_failure_is_a_tool_observation(client):
    c, token, state = client
    state.history_unavailable = True
    response = c.post(
        mcp.MCP_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": mcp.TOOL["name"], "arguments": {}},
        },
    )

    result = response.json()["result"]
    assert result["isError"] is True
    detail = json.loads(result["content"][0]["text"])
    assert detail["error"] == "history_unavailable"
    assert "Continue without it" in detail["guidance"]


def test_mcp_transport_rejects_bad_auth_and_requests(client):
    c, token, state = client
    assert c.post(mcp.MCP_PATH, json={}).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    for body, expected in [("not json", -32700), ("[]", -32600), ("x" * 16_385, -32600)]:
        assert (
            c.post(mcp.MCP_PATH, headers=headers, content=body).json()["error"]["code"] == expected
        )
    state.rate_allowed = False
    assert c.post(mcp.MCP_PATH, headers=headers, json={}).status_code == 429
    assert state.reads == 0


def test_native_mcp_client_protocol(client):
    """Optional real MCP client against a local TCP service; no Agent or external LLM."""

    executable = os.getenv("HISTORY_MCP_CLIENT_PYTHON")
    if not executable:
        pytest.skip("Set HISTORY_MCP_CLIENT_PYTHON to a Python with the official MCP client")
    c, token, _state = client
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(c.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        script = """
import asyncio, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client(os.environ["MCP_PROBE_URL"],
        headers={"Authorization": "Bearer " + os.environ["MCP_PROBE_TOKEN"]}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 1
            result = await session.call_tool("read_referenced_conversation", {})
            assert not result.isError
            assert "older" in result.content[0].text
asyncio.run(main())
print("official MCP transport client passed")
"""
        result = subprocess.run(
            [executable, "-c", script],
            env={
                **os.environ,
                "MCP_PROBE_URL": f"http://127.0.0.1:{port}{mcp.MCP_PATH}",
                "MCP_PROBE_TOKEN": token,
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.replace(token, "[redacted]")
        assert "official MCP transport client passed" in result.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize("prefix", ["fwh1", "fwh2"])
def test_restricted_bearer_is_not_a_platform_login(prefix):
    app = create_app(Settings(runtime_adapter="mock", seed_demo=False))
    token = f"{prefix}.{uuid4()}.{uuid4()}." + "a" * 43
    # Do not start the PostgreSQL-backed lifespan for a middleware-only check.
    client = TestClient(app)
    try:
        response = client.get("/api/v1/flows", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        client.cookies.set("session", "unrelated-login")
        assert client.post(mcp.MCP_PATH, json={}).status_code == 401
    finally:
        asyncio.run(app.state.container.close())


def test_grant_migration_postgres_sql_contains_tenant_policy(monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0159_agent_history_grants.py"
    spec = importlib.util.spec_from_file_location("history_grant_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://", opts={"as_sql": True, "output_buffer": output}
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()
    upgrade = output.getvalue()
    assert "token_digest VARCHAR(64) NOT NULL" in upgrade
    assert "FORCE ROW LEVEL SECURITY" in upgrade
    assert "WITH CHECK" in upgrade
    assert "owner_user_id = current_setting" in upgrade
    assert "access_token" not in upgrade
    assert "DROP" not in upgrade
    output.seek(0)
    output.truncate()
    migration.downgrade()
    assert output.getvalue().strip() == "DROP TABLE agent_conversation_history_grants;"


def test_locked_sdk_native_plugin_secret_and_restore(client, tmp_path):
    executable = os.getenv("HISTORY_MCP_NATIVE_PYTHON")
    if not executable:
        pytest.skip("Set HISTORY_MCP_NATIVE_PYTHON to an environment built from source.lock")
    from flowweave.runtime.workspace import materialize_agent_workspace_capability_marketplace

    c, token, state = client
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = {
        "url": f"http://127.0.0.1:{port}{mcp.MCP_PATH}",
        "transport": "streamable-http",
        "headers": {"Authorization": "Bearer ${FLOWWEAVE_HISTORY_ACCESS}"},
    }
    host = tmp_path / "capabilities"
    plugin_name = materialize_agent_workspace_capability_marketplace(
        {
            "capability_type": "MCP",
            "capability_key": "history-probe",
            "capability_version_id": "version-1",
            "digest": "1" * 64,
            "normalized_config": config,
        },
        host_root=host,
        runtime_root=host,
        marketplace_name="history-probe",
    )
    server = uvicorn.Server(uvicorn.Config(c.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        script = Path(__file__).parents[3] / "infra/openhands/reference_history_check.py"
        result = subprocess.run(
            [
                executable,
                str(script),
                "--marketplace",
                str(host / "marketplace"),
                "--plugin-name",
                plugin_name,
                "--state-dir",
                str(tmp_path / "native-state"),
            ],
            env={**os.environ, "HISTORY_MCP_PROBE_TOKEN": token, "OPENHANDS_SUPPRESS_BANNER": "1"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr.replace(token, "[redacted]")
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["status"] == "passed"
        assert report["events"] > 0
        source_lock = Path(__file__).parents[3] / "infra/openhands/source.lock.json"
        assert report["source_commit"] == json.loads(source_lock.read_text())["source_commit"]
        assert "restored_native_mcp_call" in report["checks"]
        assert state.reads == 2
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize("method", ["initialize", "tools/list", "ping", "tools/call"])
def test_catalog_probe_credential_never_authorizes_history(client, monkeypatch, method):
    from flowweave.modules.agent_sessions.application import history_integration, history_probe
    from flowweave.shared.settings import settings_context

    c, _token, state = client
    owner, version_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        history_integration,
        "configured_capability",
        lambda _db: SimpleNamespace(version=SimpleNamespace(id=version_id)),
    )
    with settings_context(Settings(_env_file=None)):
        token = history_probe.issue(owner, version_id)
        response = c.post(
            mcp.MCP_PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": {
                    "name": "read_referenced_conversation",
                    "arguments": {"source_binding_id": str(uuid4())},
                },
            },
        )
    assert response.status_code == (401 if method == "tools/call" else 200)
    if method == "tools/list":
        assert response.json()["result"]["tools"][0]["name"] == "read_referenced_conversation"
    assert state.reads == 0
    assert token not in response.text


def test_catalog_probe_expiry_tampering_and_version_binding(client, monkeypatch):
    from flowweave.modules.agent_sessions.application import history_integration, history_probe
    from flowweave.shared.settings import settings_context

    c, _, state = client
    owner, version_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr(history_probe.time, "time", lambda: 10_000)
    with settings_context(Settings(_env_file=None)):
        token = history_probe.issue(owner, version_id)
        assert history_probe.identity(token) == (owner, version_id)
        monkeypatch.setattr(history_probe.time, "time", lambda: 10_061)
        with pytest.raises(DomainError):
            history_probe.identity(token)
        monkeypatch.setattr(history_probe.time, "time", lambda: 10_000)
        with pytest.raises(DomainError):
            history_probe.identity(token[:-10] + "tampered00")
        monkeypatch.setattr(
            history_integration,
            "configured_capability",
            lambda _db: SimpleNamespace(version=SimpleNamespace(id=str(uuid4()))),
        )
        response = c.post(
            mcp.MCP_PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
            },
        )
        assert response.status_code == 401
    assert state.reads == 0
