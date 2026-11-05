"""MCP optional auth: real PostgreSQL isolation, references and native boundaries."""

import base64
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from flowweave.modules.catalog.application import mcp_authentication
from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeMCP
from flowweave.shared.errors import DomainError
from flowweave.shared.models import MCPAuthenticationBinding, MCPAuthenticationUse
from flowweave.shared.schemas import MCPAuthenticationWrite
from flowweave.shared.settings import settings_context

TOKEN = "synthetic-mcp-token-never-real"


def import_mcp(client, url="https://mcp.example.test/mcp", authentication=None):
    raw = json.dumps({"mcpServers": {"docs": {"url": url, "transport": "streamable-http"}}})
    checked = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "docs.json",
            "content_base64": base64.b64encode(raw.encode()).decode(),
        },
    )
    assert checked.status_code == 200, checked.text
    result = client.post(
        "/api/v1/capability-imports",
        json={
            "import_token": checked.json()["import_token"],
            **({"mcp_authentication": authentication} if authentication else {}),
        },
    )
    return result


def credential(client, host="mcp.example.test", path="/mcp", auth_type="TOKEN"):
    result = client.post(
        "/api/v1/website-credentials",
        json={
            "name": "MCP 测试认证",
            "target_host": host,
            "target_path": path,
            "auth_type": auth_type,
            "secret": TOKEN,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def bind(client, version, credential_id, expected=0):
    return client.put(
        f"/api/v1/capabilities/{version}/mcp-authentication",
        json={
            "strategy": "bearer",
            "credential_id": credential_id,
            "expected_row_version": expected,
        },
    )


def imported_id(result):
    assert result.status_code == 201, result.text
    return result.json()["capabilities"][0]["capability_id"]


def test_binding_is_user_owned_cas_and_does_not_change_shared_version(client, db_session_factory):
    version = imported_id(import_mcp(client))
    before = client.get(f"/api/v1/capabilities/{version}/mcp-source").json()
    auth = credential(client)
    saved = bind(client, version, auth["id"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["row_version"] == 1
    assert TOKEN not in saved.text
    assert bind(client, version, auth["id"]).status_code == 409
    assert client.get(f"/api/v1/capabilities/{version}/mcp-source").json() == before
    with db_session_factory() as db:
        with tenant_user(str(uuid4())):
            assert mcp_authentication.read(db, version)["strategy"] == "none"
            assert list(db.scalars(select(MCPAuthenticationBinding))) == []
            with pytest.raises(DomainError) as error:
                mcp_authentication.save(
                    db, version, MCPAuthenticationWrite(strategy="bearer", credential_id=auth["id"])
                )
            assert error.value.code == "MCP_AUTHENTICATION_UNAVAILABLE"


@pytest.mark.parametrize(
    "url,host,path",
    [
        ("https://other.test/mcp", "mcp.example.test", "/"),
        ("https://mcp.example.test/mcpx", "mcp.example.test", "/mcp"),
        ("https://mcp.example.test:8443/mcp", "mcp.example.test", "/mcp"),
        ("https://user@mcp.example.test/mcp", "mcp.example.test", "/mcp"),
        ("https://mcp.example.test/mcp/%2e%2e/admin", "mcp.example.test", "/mcp"),
    ],
)
def test_binding_rejects_wrong_scope_and_ambiguous_targets(client, url, host, path):
    version = imported_id(import_mcp(client, url))
    result = bind(client, version, credential(client, host, path)["id"])
    assert result.status_code == 422
    assert result.json()["error"]["code"] in {
        "MCP_AUTHENTICATION_SCOPE",
        "MCP_AUTHENTICATION_TARGET",
    }
    assert TOKEN not in result.text


def test_import_and_binding_are_atomic(client):
    auth = credential(client, "wrong.example.test")
    result = import_mcp(
        client,
        authentication={
            "strategy": "bearer",
            "credential_id": auth["id"],
            "expected_row_version": 0,
        },
    )
    assert result.status_code == 422, result.text
    assert not [
        item
        for item in client.get("/api/v1/capabilities").json()
        if item["capability_key"] == "docs"
    ]


def test_atomic_publish_carries_only_reference_and_plaintext_never_in_source(client):
    auth = credential(client)
    version = imported_id(
        import_mcp(
            client,
            authentication={
                "strategy": "bearer",
                "credential_id": auth["id"],
                "expected_row_version": 0,
            },
        )
    )
    read = client.get(f"/api/v1/capabilities/{version}/mcp-authentication")
    assert read.json()["credential_id"] == auth["id"]
    source = client.get(f"/api/v1/capabilities/{version}/mcp-source")
    assert TOKEN not in source.text
    assert auth["id"] not in source.text


def test_native_probe_and_creation_share_reference_without_materializing_value(
    client, settings, db_session_factory
):
    auth = credential(client)
    version = imported_id(import_mcp(client))
    assert bind(client, version, auth["id"]).status_code == 200
    owner = client.get("/api/v1/auth/me").json()["id"]
    server = RuntimeMCP(
        name="docs",
        config={"url": "https://mcp.example.test/mcp", "transport": "streamable-http"},
        capability_version_id=version,
    )
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        native, secrets = mcp_authentication.prepare_server(
            db, server, consumer_id="conversation:test"
        )
        assert native.config["auth"]["strategy"] == "bearer"
        assert native.config["auth"]["value"].startswith("${FLOWWEAVE_MCP_")
        assert list(secrets.values()) == [TOKEN]
        assert TOKEN not in json.dumps(native.config)
        use = db.scalar(select(MCPAuthenticationUse))
        assert use.reference_json["credential_id"] == auth["id"]
        assert TOKEN not in json.dumps(use.reference_json)
        probe, values = mcp_authentication.prepare_server(db, server, probe=True)
        assert probe.config["auth"]["value"] == TOKEN
        assert not values
        db.commit()
        # Credential changes never silently rebind an already-frozen consumer.
        record = db.get(WebsiteCredential, auth["id"])
        record.row_version += 1
        db.flush()
        with pytest.raises(DomainError) as failure:
            mcp_authentication.prepare_server(db, server, consumer_id="conversation:test")
        assert failure.value.code == "MCP_AUTHENTICATION_CHANGED"
        new, _ = mcp_authentication.prepare_server(db, server, consumer_id="conversation:new")
        assert new.config["auth"]["strategy"] == "bearer"


def test_clearing_auth_does_not_rebind_existing_consumer(client, settings, db_session_factory):
    version = imported_id(import_mcp(client))
    auth = credential(client)
    bind(client, version, auth["id"])
    owner = client.get("/api/v1/auth/me").json()["id"]
    server = RuntimeMCP(
        name="docs", config={"url": "https://mcp.example.test/mcp"}, capability_version_id=version
    )
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        mcp_authentication.prepare_server(db, server, consumer_id="conversation:old")
        db.commit()
    clear = client.put(
        f"/api/v1/capabilities/{version}/mcp-authentication",
        json={"strategy": "none", "expected_row_version": 1},
    )
    assert clear.status_code == 200, clear.text
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        old, _ = mcp_authentication.prepare_server(db, server, consumer_id="conversation:old")
        new, _ = mcp_authentication.prepare_server(db, server, consumer_id="conversation:new")
        assert "auth" in old.config and "auth" not in new.config


def test_binding_revalidates_deleted_or_retargeted_credential(client, settings, db_session_factory):
    version = imported_id(import_mcp(client))
    auth = credential(client)
    bind(client, version, auth["id"])
    owner = client.get("/api/v1/auth/me").json()["id"]
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        item = db.get(WebsiteCredential, auth["id"])
        item.target_host = "other.example.test"
        db.commit()
        assert mcp_authentication.read(db, version)["valid"] is False
        with pytest.raises(DomainError) as failure:
            mcp_authentication.prepare_server(
                db,
                RuntimeMCP(
                    name="docs",
                    config={"url": "https://mcp.example.test/mcp"},
                    capability_version_id=version,
                ),
                probe=True,
            )
        assert failure.value.code == "MCP_AUTHENTICATION_SCOPE"
        db.delete(item)
        db.commit()
        assert mcp_authentication.read(db, version)["valid"] is False


def test_auth_tables_have_forced_rls_and_owner_policy(client, db_session_factory):
    with db_session_factory() as db:
        for name in ("mcp_authentication_bindings", "mcp_authentication_uses"):
            row = db.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"
                ),
                {"name": name},
            ).one()
            assert row == (True, True)
            policy = db.execute(
                text("SELECT qual, with_check FROM pg_policies WHERE tablename=:name"),
                {"name": name},
            ).one()
            assert all(
                "flowweave.user_id" in clause and "owner_user_id" in clause for clause in policy
            )


def test_workspace_creation_registration_and_catalog_probe_use_same_owned_binding(
    client, settings, db_session_factory, container, monkeypatch
):
    from test_agent_workspaces import _ready_workspace_for_conversation

    from flowweave.modules.agent_sessions.application import conversations
    from flowweave.runtime.base import RuntimeMCPProbeResult, RuntimeProvider
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.runtime.mock import MockRuntime
    from flowweave.shared.artifact_store import artifact_store_context
    from flowweave.shared.schemas import MCPReadOnlyToolCallWrite

    auth = credential(client)
    version = imported_id(import_mcp(client))
    assert bind(client, version, auth["id"]).status_code == 200
    owner = client.get("/api/v1/auth/me").json()["id"]

    class Capture(MockRuntime):
        created = None
        updated = None
        probe_request = None

        def create_conversation(self, request):
            self.created = request
            return super().create_conversation(request)

        def update_conversation_secrets(self, handle, values, **kwargs):
            self.updated = values

        def probe_mcp(self, request):
            self.probe_request = request
            return RuntimeMCPProbeResult(
                ok=True,
                tools=("search_files",),
                tool_call_is_error=False,
                tool_call_text=json.dumps({"code": 0, "document": TOKEN}),
            )

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="synthetic",
        ),
    )
    runtime = Capture()
    with (
        settings_context(settings),
        tenant_user(owner),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
        runtime_context(runtime),
    ):
        workspace = _ready_workspace_for_conversation(db)
        conversations.replace_workspace_capabilities(db, workspace.id, (version,))
        conversations.create_conversation(
            db, workspace.id, "认证 MCP", workspace.default_model_provider_id, "with-auth"
        )
        assert runtime.created.agent_spec.mcp_servers[0].capability_version_id == version
        assert (
            runtime.created.agent_spec.mcp_servers[0]
            .config["auth"]["value"]
            .startswith("${FLOWWEAVE_MCP_")
        )
        assert TOKEN in runtime.created.conversation_secrets.values()
        assert not any(
            TOKEN in file.read_text(errors="ignore")
            for file in settings.workspace_root.rglob("*.json")
        )
        conversations.replace_workspace_capabilities(db, workspace.id, ())
        target = conversations.create_conversation(
            db, workspace.id, "首次装配", workspace.default_model_provider_id, "register-auth"
        )
        result = conversations.register_conversation_capabilities(
            db, workspace.id, target["id"], (version,)
        )
        assert result["completed"] is True
        assert TOKEN in runtime.updated.values()
        assert not any(
            TOKEN in file.read_text(errors="ignore")
            for file in settings.workspace_root.rglob("*.json")
        )
        result = conversations.probe_workspace_mcp_readiness(
            db,
            workspace.id,
            version,
            read_only_tool_call=MCPReadOnlyToolCallWrite(
                name="search_files", arguments={"keyword": "test"}
            ),
        )
        assert result["read_only_tool_call"] == {
            "name": "search_files",
            "is_error": False,
            "business_code": 0,
        }
        assert TOKEN not in json.dumps(result)
        assert runtime.probe_request.server.config["auth"]["value"] == TOKEN


def test_postgresql_rls_hides_bindings_and_use_audit_from_other_user(
    client, settings, db_session_factory
):
    auth = credential(client)
    version = imported_id(import_mcp(client))
    assert bind(client, version, auth["id"]).status_code == 200
    owner = client.get("/api/v1/auth/me").json()["id"]
    with settings_context(settings), tenant_user(owner), db_session_factory() as db:
        mcp_authentication.prepare_server(
            db,
            RuntimeMCP(
                name="docs",
                config={"url": "https://mcp.example.test/mcp"},
                capability_version_id=version,
            ),
            consumer_id="conversation:rls",
        )
        db.commit()
    engine = db_session_factory.kw["bind"]
    role = "mcp_probe_" + uuid4().hex
    with engine.connect() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        connection.execute(
            text(
                f'GRANT SELECT ON mcp_authentication_bindings, mcp_authentication_uses TO "{role}"'
            )
        )
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        for user, expected in ((str(uuid4()), 0), (owner, 1)):
            connection.execute(
                text("SELECT set_config('flowweave.user_id', :owner, true)"), {"owner": user}
            )
            for table in ("mcp_authentication_bindings", "mcp_authentication_uses"):
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == expected
        connection.rollback()


def test_migration_roundtrip_keeps_existing_credentials_and_capabilities(client, settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    auth = credential(client)
    version = imported_id(import_mcp(client))
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.downgrade(config, "0185_message_submission_cancel")
    command.upgrade(config, "head")
    assert (
        client.get(f"/api/v1/capabilities/{version}/mcp-authentication").json()["row_version"] == 0
    )
    assert bind(client, version, auth["id"]).status_code == 200
