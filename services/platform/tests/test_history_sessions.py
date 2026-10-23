"""Persist real leases and allowlists; native I/O is tested through bounded callbacks."""

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
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from flowweave.modules.agent_sessions.application import (
    conversations,
    history_access,
    history_sessions,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentHistorySession,
    AgentHistorySource,
)
from flowweave.modules.agent_sessions.presentation import history_mcp as mcp
from flowweave.modules.agent_workspaces.presentation import router as workspace_router
from flowweave.modules.users.application.security import current_user_id, tenant_user
from flowweave.modules.users.infrastructure.models import LdapAgentSessionOrganizationGrant, User
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch
from flowweave.shared import credentials_crypto
from flowweave.shared.errors import DomainError
from flowweave.shared.secret_redaction import redact_secret_text


@pytest.fixture(autouse=True)
def database():
    """No external database; PostgreSQL DDL is verified separately as offline SQL."""


@pytest.fixture
def graph(monkeypatch):
    owner = str(uuid4())
    bindings = [
        SimpleNamespace(
            id=str(uuid4()),
            owner_user_id=owner,
            lifecycle="ACTIVE",
            host_kind="AGENT_WORKSPACE",
            host_id="workspace",
            openhands_conversation_id=str(uuid4()),
            runtime_session_id=str(uuid4()),
        )
        for _ in range(4)
    ]
    target, first, second, outside = bindings
    by_id = {item.id: item for item in bindings}
    locks = []

    def resolve(db, workspace, target_id, source_id, *, lock_target=False):
        locks.append(lock_target)
        if workspace != "workspace" or any(
            item not in by_id or by_id[item].owner_user_id != current_user_id()
            for item in (target_id, source_id)
        ):
            raise DomainError("NOT_FOUND", "not found", 404)
        return by_id[target_id], by_id[source_id]

    monkeypatch.setattr(conversations, "history_reference_bindings", resolve)
    # One stable process key, like the configured platform master key.
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        credentials_crypto, "get_settings", lambda: SimpleNamespace(credentials_master_key=key)
    )
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def config(connection, _record):
        connection.create_function("set_config", 3, lambda *_args: "")

    for model in (AgentHistorySession, AgentHistorySource, User, LdapAgentSessionOrganizationGrant):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db, tenant_user(owner):
        user = User(id=owner, username="owner", role="SUPER_ADMIN", is_active=True)
        db.add(user)
        db.commit()
        yield SimpleNamespace(
            db=db,
            owner=owner,
            target=target,
            first=first,
            second=second,
            outside=outside,
            user=user,
            bindings=by_id,
            locks=locks,
        )
    engine.dispose()


def renew(g, *sources, ttl=900):
    return history_sessions.renew(g.db, "workspace", g.target.id, sources or (g.first.id,), ttl)


def source(g, token, identity=None):
    return history_access.authorize(g.db, token, source_binding_id=identity or g.first.id)


def test_renewal_reuses_encrypted_credential_and_only_renews_explicit_sources(graph):
    g = graph
    lease, token = renew(g)
    assert history_access.authenticate(g.db, token).id == lease.id
    assert source(g, token).source_binding_id == g.first.id
    assert token.startswith("fwh2.")
    assert token not in redact_secret_text(f"tool error: {token}")
    assert token.encode() not in lease.encrypted_token
    assert token not in repr(lease.__dict__)
    assert lease.token_digest == hashlib.sha256(token.encode()).hexdigest()
    assert credentials_crypto.decrypt_secret(lease.encrypted_token) == token
    encrypted = lease.encrypted_token
    previous = g.db.scalar(select(AgentHistorySource))
    previous.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    g.db.commit()
    with pytest.raises(DomainError):
        history_access.authenticate(g.db, token)
    again, second_token = renew(g, g.second.id)
    assert again.id == lease.id and second_token == token
    assert again.encrypted_token == encrypted
    assert source(g, token, g.second.id).source_binding_id == g.second.id
    with pytest.raises(DomainError):
        source(g, token)  # Renewing another source does not resurrect this expired source.
    assert renew(g)[1] == token
    assert source(g, token).source_binding_id == g.first.id
    assert any(g.locks)
    assert len(g.db.scalars(select(AgentHistorySession)).all()) == 1
    assert len(g.db.scalars(select(AgentHistorySource)).all()) == 2


def test_source_is_required_and_cannot_escape_the_allowlist(graph):
    _, token = renew(graph)
    for identity in (None, graph.outside.id, str(uuid4())):
        with pytest.raises(DomainError):
            history_access.authorize(graph.db, token, source_binding_id=identity)


@pytest.mark.parametrize("part", ["owner", "session", "nonce"])
def test_forged_credential_never_authorizes_a_source(graph, part):
    _, token = renew(graph)
    pieces = token.split(".")
    index = {"owner": 1, "session": 2, "nonce": 3}[part]
    pieces[index] = str(uuid4()) if index < 3 else "b" * 43
    with pytest.raises(DomainError):
        source(graph, ".".join(pieces))


@pytest.mark.parametrize("side", ["target", "first"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("openhands_conversation_id", "changed-native"),
        ("runtime_session_id", "changed-runtime"),
        ("owner_user_id", "other"),
        ("host_kind", "FLOW_NODE"),
        ("host_id", "other-workspace"),
        ("lifecycle", "DELETE_PENDING"),
    ],
)
def test_formal_identity_and_owner_changes_fence_access(graph, side, field, value):
    _, token = renew(graph)
    setattr(getattr(graph, side), field, value)
    with pytest.raises(DomainError):
        source(graph, token)
    with pytest.raises(DomainError):
        renew(graph)


def test_source_expiry_and_revocation_do_not_affect_other_sources(graph):
    g = graph
    _, token = renew(g, g.first.id, g.second.id)
    row = g.db.scalar(
        select(AgentHistorySource).where(AgentHistorySource.source_binding_id == g.first.id)
    )
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DomainError):
        source(g, token)
    assert source(g, token, g.second.id)
    renew(g)
    history_sessions.revoke(g.db, "workspace", g.target.id, g.first.id)
    assert history_access.authenticate(g.db, token)
    with pytest.raises(DomainError):
        source(g, token)
    with pytest.raises(DomainError):
        renew(g)
    assert source(g, token, g.second.id)
    history_sessions.revoke(g.db, "workspace", g.target.id)
    with pytest.raises(DomainError):
        history_access.authenticate(g.db, token)
    with pytest.raises(DomainError):
        renew(g, g.second.id)


@pytest.mark.parametrize("change", ["disabled", "permission", "wrong-owner", "ciphertext"])
def test_renewal_requires_current_authority_and_decryptable_credential(graph, change):
    g = graph
    lease, token = renew(g)
    if change == "disabled":
        g.user.is_active = False
    elif change == "permission":
        g.user.role = "USER"
        g.user.auth_source = "LDAP"
    elif change == "ciphertext":
        lease.encrypted_token = b"corrupted"
    if change == "wrong-owner":
        with tenant_user(str(uuid4())), pytest.raises(DomainError):
            renew(g)
    else:
        with pytest.raises(DomainError):
            renew(g)
    if change in {"disabled", "permission"}:
        with pytest.raises(DomainError):
            source(g, token)


@pytest.mark.parametrize(
    "sources,ttl",
    [
        ([], 900),
        (["duplicate", "duplicate"], 900),
        (["source"], 59),
        (["source"], 3601),
        (["source"], True),
    ],
)
def test_invalid_renewal_does_not_create_credentials(graph, sources, ttl):
    identities = tuple(
        graph.first.id if value in {"source", "duplicate"} else value for value in sources
    )
    with pytest.raises(DomainError) as caught:
        history_sessions.renew(graph.db, "workspace", graph.target.id, identities, ttl)
    assert caught.value.code == (
        "AGENT_HISTORY_TTL_INVALID" if sources == ["source"] else "AGENT_HISTORY_SOURCES_INVALID"
    )
    assert not graph.db.scalars(select(AgentHistorySession)).all()


def test_recovered_ciphertext_cannot_be_substituted_from_another_target(graph):
    g = graph
    lease, _ = renew(g)
    other, _ = history_sessions.renew(g.db, "workspace", g.outside.id, (g.second.id,))
    lease.encrypted_token = other.encrypted_token
    lease.token_digest = other.token_digest
    # Keep the corrupt fixture in memory; do not turn its duplicate digest into a DB error.
    with g.db.no_autoflush, pytest.raises(DomainError):
        renew(g)


@pytest.mark.parametrize("side", ["target", "first"])
def test_binding_deletion_removes_only_its_history_authority(graph, side):
    g = graph
    _, token = renew(g, g.first.id, g.second.id)
    history_sessions.delete_binding_access(g.db, getattr(g, side).id)
    with pytest.raises(DomainError):
        source(g, token)
    if side == "first":
        assert source(g, token, g.second.id)
        assert len(g.db.scalars(select(AgentHistorySession)).all()) == 1
    else:
        assert not g.db.scalars(select(AgentHistorySession)).all()
        assert not g.db.scalars(select(AgentHistorySource)).all()


def test_projection_rechecks_source_revocation_after_native_read(graph, monkeypatch):
    g = graph
    _, token = renew(g)
    monkeypatch.setattr(conversations, "prepare_conversation_events", lambda *_args: object())
    monkeypatch.setattr(
        conversations, "assert_prepared_conversation_events_current", lambda *_args: None
    )
    args = mcp.HistoryArguments(source_binding_id=g.first.id, event_id="formal-event")
    page = mcp.prepare_page(g.db, token, args)
    batch = RuntimeEventBatch(
        events=(
            RuntimeEvent(
                cursor="formal-event",
                event_type="MESSAGE",
                payload={"source": "assistant", "content": "private"},
            ),
        )
    )
    history_sessions.revoke(g.db, "workspace", g.target.id, g.first.id)
    with pytest.raises(DomainError):
        mcp.project_page(g.db, page, batch, token, args)


def test_target_lock_is_requested_only_for_control_plane_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(conversations, "_workspace", lambda *_args: SimpleNamespace(id="workspace"))

    def binding(_db, _workspace, identity, **kwargs):
        calls.append((identity, kwargs))
        return identity

    monkeypatch.setattr(conversations, "_binding", binding)
    conversations.history_reference_bindings(
        None, "workspace", "target", "source", lock_target=True
    )
    assert calls == [("target", {"lock": True}), ("source", {})]


def test_session_migration_postgres_sql(monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0160_agent_history_sessions.py"
    spec = importlib.util.spec_from_file_location("history_session_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://", opts={"as_sql": True, "output_buffer": output}
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()
    sql = output.getvalue()
    assert sql.count("FORCE ROW LEVEL SECURITY") == 2
    assert sql.count("WITH CHECK") == 2
    assert "encrypted_token BYTEA NOT NULL" in sql
    assert "UNIQUE (owner_user_id, target_binding_id)" in sql
    assert "UNIQUE (session_id, source_binding_id)" in sql
    assert "access_token" not in sql
    output.seek(0)
    output.truncate()
    migration.downgrade()
    assert output.getvalue().strip().splitlines() == [
        "DROP TABLE agent_history_sources;",
        "",
        "DROP TABLE agent_history_sessions;",
    ]


@pytest.fixture
def history_app(graph, monkeypatch):
    from flowweave.shared.http import get_db

    g = graph
    reads = []

    async def control(_container, operation):
        with tenant_user(g.owner):
            return operation(g.db)

    async def run(_db, operation):
        with tenant_user(g.owner):
            result = operation(g.db)
            g.db.commit()
            return result

    async def events(_container, prepare, read, project, *, history):
        assert history is True
        with tenant_user(g.owner):
            page = prepare(g.db)
            g.db.commit()
            batch = read(page)
            result = project(g.db, page, batch)
            g.db.commit()
            return result

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(allowed=True)

    def native(prepared):
        reads.append(prepared.source)
        return RuntimeEventBatch(
            events=(
                RuntimeEvent(
                    cursor="native-event",
                    event_type="MESSAGE",
                    payload={"source": "assistant", "content": "受限历史正文"},
                ),
            )
        )

    monkeypatch.setattr(workspace_router, "run_sync", run)
    monkeypatch.setattr(mcp, "run_blocking_control", control)
    monkeypatch.setattr(mcp, "run_formal_events", events)
    monkeypatch.setattr(
        conversations,
        "prepare_conversation_events",
        lambda _db, _workspace, source_id, *_args: SimpleNamespace(source=source_id),
    )
    monkeypatch.setattr(conversations, "read_prepared_conversation_events", native)
    monkeypatch.setattr(
        conversations, "assert_prepared_conversation_events_current", lambda *_args: None
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(rate_limiter=SimpleNamespace(allow=allow))
    app.dependency_overrides[get_db] = lambda: None
    app.include_router(workspace_router.router)
    app.include_router(mcp.router)

    @app.exception_handler(DomainError)
    async def error(_request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    return app, g, reads


def test_http_renew_query_and_revoke_with_real_authority(history_app):
    """Real HTTP schema/dispatch and SQLite authorization; only native I/O is controlled."""
    app, g, reads = history_app
    path = f"/agent-workspaces/workspace/conversations/{g.target.id}/history-session"
    with TestClient(app) as client:
        response = client.post(path, json={"source_binding_ids": [g.first.id]})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        def rpc(method, arguments=None):
            params = (
                {"name": mcp.TOOL["name"], "arguments": arguments or {}}
                if method == "tools/call"
                else {}
            )
            return client.post(
                mcp.MCP_PATH,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )

        assert rpc("initialize").status_code == 200
        assert rpc("tools/list").status_code == 200
        assert rpc("tools/call").status_code == 401
        assert rpc("tools/call", {"source_binding_id": g.outside.id}).status_code == 401
        assert not reads
        result = rpc("tools/call", {"source_binding_id": g.first.id})
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        data = json.loads(result.json()["result"]["content"][0]["text"])
        assert data["source_binding_id"] == g.first.id
        assert data["messages"][0]["content"] == "受限历史正文"
        assert reads == [g.first.id]
        again = client.post(path, json={"source_binding_ids": [g.second.id]})
        assert again.json()["access_token"] == token
        assert rpc("tools/call", {"source_binding_id": g.second.id}).status_code == 200
        assert client.delete(path + f"/sources/{g.first.id}").status_code == 204
        assert rpc("tools/call", {"source_binding_id": g.first.id}).status_code == 401
        assert rpc("tools/call", {"source_binding_id": g.second.id}).status_code == 200
        assert reads == [g.first.id, g.second.id, g.second.id]
        assert client.delete(path).status_code == 204
        assert rpc("tools/list").status_code == 401
        assert client.post(path, json={"source_binding_ids": [g.second.id]}).status_code == 401


def test_locked_sdk_session_credential_queries_two_sources_after_restore(history_app, tmp_path):
    executable = os.getenv("HISTORY_MCP_NATIVE_PYTHON")
    if not executable:
        pytest.skip("Set HISTORY_MCP_NATIVE_PYTHON to an environment built from source.lock")
    from flowweave.runtime.workspace import materialize_agent_workspace_capability_marketplace

    app, g, reads = history_app
    _, token = renew(g)
    assert renew(g, g.second.id)[1] == token
    g.db.commit()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    host = tmp_path / "capabilities"
    name = materialize_agent_workspace_capability_marketplace(
        {
            "capability_type": "MCP",
            "capability_key": "history-probe",
            "capability_version_id": "version-1",
            "digest": "1" * 64,
            "normalized_config": {
                "url": f"http://127.0.0.1:{port}{mcp.MCP_PATH}",
                "transport": "streamable-http",
                "headers": {"Authorization": "Bearer ${FLOWWEAVE_HISTORY_ACCESS}"},
            },
        },
        host_root=host,
        runtime_root=host,
        marketplace_name="history-probe",
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
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
                name,
                "--state-dir",
                str(tmp_path / "native-state"),
                "--source-binding-id",
                g.first.id,
                "--restored-source-binding-id",
                g.second.id,
            ],
            env={**os.environ, "HISTORY_MCP_PROBE_TOKEN": token},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr.replace(token, "[redacted]")
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["status"] == "passed"
        assert "restored_native_mcp_call" in report["checks"]
        assert reads == [g.first.id, g.second.id]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
