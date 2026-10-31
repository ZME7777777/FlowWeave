"""Real PostgreSQL, governed capability artifacts and HTTP tool authorization."""

import hashlib
import json
import socket
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkDirectoryVersion
from flowweave.modules.cases.application import context_tools, native_tools
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlRound,
    CaseStage,
    CaseToolGrant,
)
from flowweave.modules.catalog.infrastructure.models import (
    CapabilityBlob,
    CapabilityPackage,
    CapabilityVersion,
)
from flowweave.modules.users.application.security import tenant_user
from flowweave.modules.users.infrastructure.models import User
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.credentials_crypto import decrypt_secret
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import settings_context


def publish_case_tools(
    db, settings, store, monkeypatch, url=None, *, contract=native_tools.CONTEXT
):
    config = {
        "url": url or "http://case-tools.example.test/api/v1/case-tools-mcp",
        "transport": "streamable-http",
        "headers": {"Authorization": contract.auth_header},
    }
    content = json.dumps({"mcpServers": {contract.server_name: config}}).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    package = db.scalar(
        select(CapabilityPackage).where(
            CapabilityPackage.capability_type == "MCP",
            CapabilityPackage.capability_key == contract.server_name,
        )
    )
    if package is None:
        package = CapabilityPackage(
            capability_type="MCP", capability_key=contract.server_name, display_name="Case context"
        )
    blob = CapabilityBlob(
        content_hash=content_hash,
        storage_key=contract.server_name + "/" + content_hash + ".json",
        byte_size=len(content),
        media_type="application/json",
    )
    store.put(blob.storage_key, content)
    db.add_all([package, blob])
    db.flush()
    version = CapabilityVersion(
        package_id=package.id,
        blob_id=blob.id,
        version_no=(
            db.scalar(
                select(func.max(CapabilityVersion.version_no)).where(
                    CapabilityVersion.package_id == package.id
                )
            )
            or 0
        )
        + 1,
        digest=capability_version_digest("MCP", contract.server_name, content_hash, config),
        normalized_config_json=config,
        source_filename=contract.server_name + ".json",
        state="PUBLISHED",
    )
    db.add(version)
    db.commit()
    configured = settings.model_copy(
        update={
            contract.setting_prefix + "_url": config["url"],
            contract.setting_prefix + "_capability_version_id": version.id,
        }
    )
    monkeypatch.setattr(native_tools, "get_settings", lambda: configured)
    if contract == native_tools.CONTEXT:
        publish_case_tools(
            db,
            configured,
            store,
            monkeypatch,
            config["url"].removesuffix(native_tools.MCP_PATH) + native_tools.FLOWS.path,
            contract=native_tools.FLOWS,
        )
    if contract == native_tools.FLOWS:
        publish_case_tools(
            db,
            configured,
            store,
            monkeypatch,
            config["url"].removesuffix(native_tools.FLOWS.path) + native_tools.MATERIALS.path,
            contract=native_tools.MATERIALS,
        )
    return version


def test_case_mcp_versions_are_hidden_and_rejected_by_generic_sessions(
    client, db_session_factory, settings, container, monkeypatch
):
    owner = client.get("/api/v1/auth/me").json()["id"]
    with (
        tenant_user(owner),
        settings_context(settings),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
    ):
        context_version = publish_case_tools(db, settings, container.artifact_store, monkeypatch)
        package_id = context_version.package_id
        workspace = conversations.default_workspace(db)
        with pytest.raises(DomainError) as raised:
            conversations.replace_workspace_capabilities(db, workspace["id"], (context_version.id,))
        assert raised.value.code == "CAPABILITY_PLATFORM_MANAGED"
        assert native_tools._published(db, context_version.id).version.id == context_version.id

    listed = client.get("/api/v1/capabilities")
    assert listed.status_code == 200
    assert not any(item["lineage_id"] == package_id for item in listed.json())
    directory = client.get("/api/v1/capability-directory", params={"capability_type": "MCP"})
    assert directory.status_code == 200
    assert all(
        item["latest"]["capability_key"]
        not in {
            native_tools.CONTEXT.server_name,
            native_tools.FLOWS.server_name,
            native_tools.MATERIALS.server_name,
        }
        for item in directory.json()["items"]
    )


@pytest.fixture
def native_case(client, db_session_factory, settings, container, monkeypatch, request):
    owner = client.get("/api/v1/auth/me").json()["id"]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    request.addfinalizer(listener.close)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}{native_tools.MCP_PATH}"
    with (
        tenant_user(owner),
        settings_context(settings),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
    ):
        version = publish_case_tools(db, settings, container.artifact_store, monkeypatch, url)
        directory_id, workspace_id = str(uuid4()), str(uuid4())
        directory = AgentWorkDirectoryVersion(
            work_directory_id=directory_id, version=1, working_path=".flowweave/cases/test"
        )
        db.add(directory)
        db.flush()
        binding = AgentConversationBinding(
            workspace_id=workspace_id,
            host_id=workspace_id,
            conversation_scope_id=workspace_id,
            runtime_session_id=str(uuid4()),
            openhands_conversation_id=str(uuid4()),
            work_directory_version_id=directory.id,
            working_directory="/runtime/workspace/project/.flowweave/cases/test",
            lifecycle="PROVISIONING",
            create_idempotency_key="native-case-test",
        )
        db.add(binding)
        db.flush()
        item = Case(
            name="Case own",
            objective="own objective",
            state="ACTIVE",
            control_work_directory_id=directory_id,
            control_conversation_binding_id=binding.id,
            materials_json=[
                {"kind": "LINK", "name": "reference", "value": "https://example.test/ref"},
                {"kind": "FILE", "name": "facts.md", "value": ".flowweave/cases/test/facts.md"},
            ],
        )
        db.add(item)
        db.flush()
        entry = CaseControlRound(
            case_id=item.id,
            workspace_id=workspace_id,
            work_directory_id=directory_id,
            binding_id=binding.id,
            idempotency_key="round",
            state="PENDING",
            initial_prompt="own scene",
        )
        db.add(entry)
        db.flush()
        item.current_control_round_id = entry.id
        db.add_all(
            [
                CaseStage(
                    case_id=item.id,
                    stage_key=str(n),
                    name=f"stage {n}",
                    position=n,
                    state="ACTIVE" if n == 0 else "PENDING",
                )
                for n in range(3)
            ]
        )
        other = Case(name="Other scene", objective="must not leak", state="ACTIVE")
        db.add(other)
        db.commit()
        token = native_tools.prepare_creation(db, entry.id, binding)[native_tools.SECRET_NAME]
        yield SimpleNamespace(
            db=db,
            owner=owner,
            item=item,
            entry=entry,
            binding=binding,
            token=token,
            version=version,
            other=other,
            client=client,
            listener=listener,
        )


def rpc(g, method, params=None, *, token=None):
    return g.client.post(
        native_tools.MCP_PATH,
        headers={"Authorization": "Bearer " + (token or g.token)},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def test_native_initialize_and_reads_use_scoped_credential_and_bounded_pages(native_case):
    g = native_case
    assert (
        rpc(g, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]["protocolVersion"]
        == "2025-06-18"
    )
    tools = rpc(g, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["read_case_context"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    overview = rpc(g, "tools/call", {"name": "read_case_context"})
    assert overview.status_code == 200
    body = overview.json()["result"]["content"][0]["text"]
    assert "own objective" in body and "must not leak" not in body
    assert g.token not in body and "credential" not in body and "session_config" not in body
    first = context_tools.read_context(
        g.db, g.token, context_tools.ContextArguments(section="stages", limit=2)
    )
    second = context_tools.read_context(
        g.db,
        g.token,
        context_tools.ContextArguments(section="stages", offset=first["next_offset"], limit=2),
    )
    assert [x["name"] for x in first["items"] + second["items"]] == [
        "stage 0",
        "stage 1",
        "stage 2",
    ]
    assert second["next_offset"] is None
    materials = context_tools.read_context(
        g.db, g.token, context_tools.ContextArguments(section="materials")
    )
    assert len(materials["items"]) == 2
    assert materials["items"][1]["name"] == "facts.md"


def test_retry_reuses_credential_and_frozen_version_after_default_change(
    native_case, monkeypatch, settings
):
    g = native_case
    changed = settings.model_copy(
        update={
            "case_tools_mcp_capability_version_id": str(uuid4()),
            "case_tools_mcp_url": "https://changed.example.test/",
        }
    )
    monkeypatch.setattr(native_tools, "get_settings", lambda: changed)
    assert (
        native_tools.prepare_creation(g.db, g.entry.id, g.binding)[native_tools.SECRET_NAME]
        == g.token
    )
    grant = g.db.scalar(select(CaseToolGrant))
    assert decrypt_secret(grant.encrypted_token) == g.token
    assert g.token.encode() not in grant.encrypted_token
    assert g.token not in json.dumps(grant.runtime_manifest_json)
    assert grant.runtime_manifest_json["capabilities"][0]["version_id"] == g.version.id
    assert len(g.db.scalars(select(CaseToolGrant)).all()) == 1
    assert len(g.db.scalars(select(AgentConversationCapability)).all()) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"case_id": "another"},
        {"owner_user_id": "another"},
        {"binding_id": "another"},
        {"flow_run_id": "template"},
        {"section": "write"},
        {"limit": True},
        {"limit": 51},
        {"offset": -1},
        {"round_id": "old"},
    ],
)
def test_agent_cannot_supply_authority_or_write_arguments(native_case, arguments):
    response = rpc(native_case, "tools/call", {"name": "read_case_context", "arguments": arguments})
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "field,value",
    [
        ("round.state", "FAILED"),
        ("round.state", "DELETED"),
        ("case.state", "COMPLETED"),
        ("case.state", "ARCHIVED"),
        ("case.current_control_round_id", "random"),
        ("case.control_conversation_binding_id", "random"),
        ("case.control_work_directory_id", "random"),
        ("binding.lifecycle", "DELETE_PENDING"),
        ("binding.runtime_session_id", "random"),
        ("binding.openhands_conversation_id", "random"),
        ("binding.host_kind", "FLOW_NODE"),
        ("binding.host_id", "random"),
        ("binding.workspace_id", "random"),
        ("binding.work_directory_version_id", "random"),
    ],
)
def test_stale_historical_or_drifted_coordinator_is_denied(native_case, field, value):
    g = native_case
    target, column = field.split(".")
    setattr({"round": g.entry, "case": g.item, "binding": g.binding}[target], column, value)
    g.db.commit()
    with pytest.raises(DomainError, match="场景工具授权"):
        native_tools.authenticate(g.db, g.token)


def test_credential_required_even_with_admin_cookie_and_forged_owner_fails(native_case):
    g = native_case
    assert (
        g.client.post(
            native_tools.MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).status_code
        == 401
    )
    owner, grant = native_tools.token_identity(g.token)
    forged = g.token.replace(owner, str(uuid4()), 1)
    assert rpc(g, "tools/list", token=forged).status_code == 401
    tampered = g.token[:-1] + ("A" if g.token[-1] != "A" else "B")
    assert rpc(g, "tools/list", token=tampered).status_code == 401
    assert rpc(g, "tools/list", token="fwh2." + g.token.split(".", 1)[1]).status_code == 401
    g.db.get(User, owner).is_active = False
    g.db.commit()
    assert rpc(g, "tools/list").status_code == 401


def test_paused_case_still_allows_reads_and_latest_materials(native_case):
    g = native_case
    g.item.state = "PAUSED"
    g.item.materials_json = [{"kind": "LINK", "name": "new", "value": "https://example.test/new"}]
    g.db.commit()
    result = context_tools.read_context(
        g.db, g.token, context_tools.ContextArguments(section="materials")
    )
    assert result["items"][0]["name"] == "new"


def test_missing_or_corrupted_governed_capability_fails_closed(native_case, container):
    g = native_case
    grant = g.db.scalar(select(CaseToolGrant))
    blob = g.db.get(CapabilityBlob, g.version.blob_id)
    container.artifact_store.put(blob.storage_key, b"corrupted")
    with pytest.raises(DomainError) as caught:
        native_tools.prepare_creation(g.db, g.entry.id, g.binding)
    assert caught.value.code == "CASE_TOOL_CAPABILITY_UNAVAILABLE"
    assert g.db.get(CaseToolGrant, grant.id).capability_version_id == g.version.id


def test_binding_delete_revokes_and_removes_credential(native_case):
    g = native_case
    grant_id = g.db.scalar(select(CaseToolGrant.id))
    g.db.delete(g.binding)
    g.db.commit()
    assert g.db.get(CaseToolGrant, grant_id, populate_existing=True) is None
    with pytest.raises(DomainError):
        native_tools.authenticate(g.db, g.token)


def test_grant_migration_enforces_rls_and_cascades_without_deleting_native_history(
    db_session_factory,
):
    source = Path(__file__).parents[1] / "migrations/versions/0181_case_tool_grants.py"
    spec = spec_from_file_location("case_tool_grant_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema, role = "case_grant_test_" + uuid4().hex, "case_grant_reader_" + uuid4().hex
    with db_session_factory.kw["bind"].connect() as connection, connection.begin():
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(text("CREATE TABLE case_control_rounds (id varchar(36) PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE agent_conversation_bindings (id varchar(36) PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO case_control_rounds VALUES ('round')"))
        connection.execute(text("INSERT INTO agent_conversation_bindings VALUES ('binding')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            text(
                "INSERT INTO case_tool_grants (id, owner_user_id, round_id, binding_id, "
                "work_directory_version_id, runtime_session_id, native_conversation_id, "
                "token_digest, encrypted_token, "
                "capability_version_id, capability_digest, runtime_manifest_json, created_at) "
                "VALUES ('grant', 'owner', 'round', 'binding', 'directory-version', "
                "'runtime', 'native', :digest, "
                ":encrypted, 'version', :digest, '{}', now())"
            ),
            {"digest": "1" * 64, "encrypted": b"encrypted-fixture"},
        )
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, UPDATE ON case_tool_grants TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_tool_grants")) == 0
        assert (
            connection.execute(text("UPDATE case_tool_grants SET token_digest = 'bad'")).rowcount
            == 0
        )
        connection.execute(text("SELECT set_config('flowweave.user_id', 'owner', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_tool_grants")) == 1
        assert connection.scalar(text("SELECT token_digest FROM case_tool_grants")) == "1" * 64
        connection.execute(text("RESET ROLE"))
        connection.execute(text("DELETE FROM case_control_rounds WHERE id = 'round'"))
        assert connection.scalar(text("SELECT count(*) FROM case_tool_grants")) == 0
        assert connection.scalar(text("SELECT count(*) FROM agent_conversation_bindings")) == 1
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM agent_conversation_bindings")) == 1
        connection.rollback()


def test_tool_credential_literals_are_redacted(native_case):
    from flowweave.shared.secret_redaction import redact_secret_text

    token = native_case.token
    assert token not in redact_secret_text("unexpected " + token + " error")


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([], -32600),
        ({"id": 1, "method": "tools/list"}, -32600),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/list"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}, -32602),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "start_execution"},
            },
            -32602,
        ),
        ({"jsonrpc": "2.0", "id": 1, "method": "unknown"}, -32601),
    ],
)
def test_protocol_rejects_invalid_requests_and_unknown_writes(native_case, payload, expected):
    g = native_case
    response = g.client.post(
        native_tools.MCP_PATH, headers={"Authorization": "Bearer " + g.token}, json=payload
    )
    assert response.json()["error"]["code"] == expected


def test_request_body_bound_and_notifications(native_case):
    g = native_case
    headers = {"Authorization": "Bearer " + g.token}
    assert (
        g.client.post(native_tools.MCP_PATH, headers=headers, content=b"x" * 16_385).json()[
            "error"
        ]["code"]
        == -32600
    )
    assert (
        g.client.post(native_tools.MCP_PATH, headers=headers, content=b"{").json()["error"]["code"]
        == -32700
    )
    assert (
        g.client.post(
            native_tools.MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ).status_code
        == 202
    )


@pytest.mark.parametrize(
    "contract", [native_tools.CONTEXT, native_tools.FLOWS, native_tools.MATERIALS]
)
def test_locked_sdk_loads_calls_and_restores_case_context(native_case, tmp_path, contract):
    """The real locked SDK discovers and invokes tools; no LLM call is made."""
    run_locked_case_probe(native_case, tmp_path, contract)


def run_locked_case_probe(g, tmp_path, contract, action_args=None):
    import os
    import subprocess
    import threading
    import time

    import uvicorn

    from flowweave.runtime.workspace import materialize_agent_workspace_capability_marketplace

    executable = os.getenv("CASE_MCP_NATIVE_PYTHON")
    if not executable:
        pytest.skip("Set CASE_MCP_NATIVE_PYTHON to a source.lock SDK environment")
    listener = g.listener
    version = native_tools._published(g.db, contract=contract).version
    token = native_tools.prepare_creation(g.db, g.entry.id, g.binding, contract=contract)[
        contract.secret_name
    ]
    config = version.normalized_config_json
    host = tmp_path / "capabilities"
    plugin = materialize_agent_workspace_capability_marketplace(
        {
            "capability_type": "MCP",
            "capability_key": contract.server_name,
            "capability_version_id": version.id,
            "digest": version.digest,
            "normalized_config": config,
        },
        host_root=host,
        runtime_root=host,
        marketplace_name="history-probe",
    )
    server = uvicorn.Server(uvicorn.Config(g.client.app, log_level="critical", lifespan="off"))
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
                plugin,
                "--state-dir",
                str(tmp_path / "native-state"),
                "--initial-config",
                (
                    "--case-flows"
                    if contract == native_tools.FLOWS
                    else "--case-materials"
                    if contract == native_tools.MATERIALS
                    else "--case-context"
                ),
                *(["--case-flow-action-json", json.dumps(action_args)] if action_args else []),
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
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize("configured", [False, True])
def test_native_recovery_rejects_grantless_or_mismatched_existing_tools(
    native_case, monkeypatch, configured
):
    from flowweave.runtime.base import RuntimeHandle

    g = native_case
    checked = []

    def check(handle, key, url):
        checked.append((key, url))
        return configured

    monkeypatch.setattr(
        native_tools, "get_runtime", lambda: SimpleNamespace(conversation_mcp_configured=check)
    )
    handle = RuntimeHandle(job_id="test", conversation_id=g.binding.openhands_conversation_id)
    if configured:
        native_tools.assert_native_registration(g.db, g.binding.id, handle)
    else:
        with pytest.raises(DomainError) as caught:
            native_tools.assert_native_registration(g.db, g.binding.id, handle)
        assert caught.value.code == "CASE_TOOL_REGISTRATION_MISMATCH"
        assert caught.value.details["outcome_unknown"] is True
        assert g.db.get(AgentConversationBinding, g.binding.id) is not None
    assert checked == [(native_tools.SERVER_NAME, g.version.normalized_config_json["url"])]
