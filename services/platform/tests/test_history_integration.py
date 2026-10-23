"""Product integration against real governed versions/leases with controlled native I/O."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_history_sessions as session_fixtures
from sqlalchemy import event, select

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import (
    conversations,
    history_access,
)
from flowweave.modules.agent_sessions.application import (
    history_integration as integration,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationCapability,
    AgentHistorySession,
)
from flowweave.modules.catalog.infrastructure.models import (
    CapabilityBlob,
    CapabilityPackage,
    CapabilityVersion,
)
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeInputReadiness,
    RuntimeMCPProbeResult,
)
from flowweave.shared.domain.capability_digest import capability_version_digest
from flowweave.shared.errors import DomainError

graph = session_fixtures.graph


@pytest.fixture(autouse=True)
def database():
    """Use the SQLite fixture, never a production database."""


@pytest.fixture
def setup(graph, monkeypatch, tmp_path):
    g = graph
    for model in (
        AgentConversationCapability,
        CapabilityBlob,
        CapabilityPackage,
        CapabilityVersion,
    ):
        model.__table__.create(g.db.get_bind())
    config = {
        "url": "http://history.example.test/api/v1/agent-history-mcp",
        "transport": "streamable-http",
        "headers": {"Authorization": integration.AUTH_HEADER},
    }
    content = json.dumps({"mcpServers": {integration.SERVER_NAME: config}}).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    package = CapabilityPackage(
        capability_type="MCP", capability_key=integration.SERVER_NAME, display_name="History"
    )
    blob = CapabilityBlob(
        content_hash=content_hash,
        storage_key="history.json",
        byte_size=len(content),
        media_type="application/json",
    )
    g.db.add_all([package, blob])
    g.db.flush()
    version = CapabilityVersion(
        package_id=package.id,
        blob_id=blob.id,
        version_no=1,
        digest=capability_version_digest("MCP", integration.SERVER_NAME, content_hash, config),
        normalized_config_json=config,
        source_filename="history.json",
        state="PUBLISHED",
    )
    g.db.add(version)
    g.db.commit()
    settings = SimpleNamespace(
        agent_history_mcp_capability_version_id=version.id, agent_history_mcp_url=config["url"]
    )
    monkeypatch.setattr(integration, "get_settings", lambda: settings)
    monkeypatch.setattr(
        integration, "get_artifact_store", lambda: SimpleNamespace(read=lambda _key: content)
    )
    g.first.display_title = "来源会话"
    g.second.display_title = "另一个来源"
    monkeypatch.setattr(conversations, "_binding", lambda _db, _ws, identity: g.bindings[identity])
    handle = RuntimeHandle(job_id="runtime", conversation_id=g.target.openhands_conversation_id)
    monkeypatch.setattr(conversations, "history_runtime_handle", lambda *_args: handle)
    monkeypatch.setattr(
        conversations,
        "history_capability_marketplace_paths",
        lambda *_args: (tmp_path / "capabilities", tmp_path / "capabilities", "history-test"),
    )
    commits = []
    event.listen(g.db, "after_commit", lambda _db: commits.append(True))

    class Runtime:
        configured = False
        ready = True
        fail_load = False
        calls = []
        secrets = {}
        probe_ok = True

        def probe_mcp(self, request):
            assert request.server.config["headers"]["Authorization"].startswith("Bearer fwh2.")
            assert history_access.authenticate(
                g.db, request.server.config["headers"]["Authorization"][7:]
            )
            return RuntimeMCPProbeResult(
                ok=self.probe_ok, tools=("read_referenced_conversation",) if self.probe_ok else ()
            )

        def can_accept_input(self, _handle):
            return self.ready

        def reload_conversation(self, _handle):
            return SimpleNamespace(conversation_id=g.target.openhands_conversation_id)

        def conversation_mcp_configured(self, _handle, name, url):
            assert name == integration.SERVER_NAME and url == config["url"]
            return self.configured

        def update_conversation_secrets(self, _handle, secrets):
            self.secrets.update(secrets)
            self.calls.append("secrets")

        def load_plugin(self, _handle, ref):
            assert commits, "The MCP credential must be committed before loader I/O"
            token = self.secrets[integration.SECRET_NAME]
            assert history_access.authenticate(g.db, token)
            self.calls.append("load")
            if self.fail_load:
                raise DomainError("EXECUTOR_UNAVAILABLE", "uncertain native response", 504)
            self.configured = True

    runtime = Runtime()
    monkeypatch.setattr(integration, "get_runtime", lambda: runtime)
    g.config = config
    g.version = version
    g.settings = settings
    g.runtime = runtime
    g.workspace = SimpleNamespace(id="workspace")
    g.root = tmp_path / "capabilities"
    g.refs = conversations._resolve_message_references(
        g.db, g.workspace, g.target, ({"source_binding_id": g.first.id},)
    )
    return g


def test_whole_reference_is_metadata_and_does_not_read_runtime(setup, monkeypatch):
    g = setup
    monkeypatch.setattr(
        conversations, "get_runtime", lambda: pytest.fail("No history read for metadata")
    )
    refs = conversations._resolve_message_references(
        g.db, g.workspace, g.target, ({"source_binding_id": g.first.id},)
    )
    assert refs[0] == {
        "kind": "conversation",
        "event_id": "",
        "content": "来源会话",
        "title": "来源会话",
        "source_binding_id": g.first.id,
        "source_conversation_id": g.first.openhands_conversation_id,
        "history_tool": "read_referenced_conversation",
    }
    prompt, _ = conversations.message_payload("继续当前任务", (), refs)
    visible, projected, *_ = conversations.project_conversation_references(prompt)
    assert visible == "继续当前任务" and projected == refs
    assert len(prompt) < 1500


def test_native_registration_freezes_provenance_and_reuses_credential(setup):
    g = setup
    assert integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert g.runtime.calls == ["secrets", "load"]
    lease = g.db.scalar(select(AgentHistorySession))
    token = g.runtime.secrets[integration.SECRET_NAME]
    assert lease.registered_at is not None and lease.capability_version_id == g.version.id
    assert lease.runtime_manifest_json["capabilities"][0]["content_hash"]
    assert g.db.scalar(select(AgentConversationCapability)).capability_version_id == g.version.id
    manifest = json.loads((g.root / ".flowweave-manifest.json").read_text())
    assert manifest["capabilities"][0]["digest"] == g.version.digest
    for path in g.root.rglob("*.json"):
        assert token.encode() not in path.read_bytes()
    g.runtime.ready = False
    refs = conversations._resolve_message_references(
        g.db, g.workspace, g.target, ({"source_binding_id": g.second.id},)
    )
    assert integration.ensure(g.db, g.workspace, g.target, refs, allow_install=False)
    assert g.runtime.calls == ["secrets", "load"]
    assert history_access.authorize(g.db, token, source_binding_id=g.second.id)


@pytest.mark.parametrize("provisioning", [False, True])
def test_loader_response_loss_is_reconciled_without_reloading(setup, provisioning):
    g = setup
    if provisioning:
        g.target.lifecycle = "PROVISIONING"
    g.runtime.fail_load = True
    with pytest.raises(DomainError):
        integration.ensure(g.db, g.workspace, g.target, g.refs)
    g.db.rollback()
    assert g.db.scalar(select(AgentHistorySession)).registered_at is None
    g.runtime.configured = True  # Formal native config is present after a lost acknowledgement.
    g.runtime.fail_load = False
    assert integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert g.runtime.calls.count("load") == 1


def test_first_running_reference_is_not_dispatched_or_installed(setup):
    g = setup
    g.runtime.ready = False
    assert integration.ensure(g.db, g.workspace, g.target, g.refs, allow_install=False) is False
    with pytest.raises(DomainError) as caught:
        integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert caught.value.code == "AGENT_HISTORY_CAPABILITY_REQUIRES_IDLE"
    assert not g.runtime.calls


@pytest.mark.parametrize(
    "change", ["disabled", "endpoint", "headers", "digest", "blob", "type", "untrusted-native"]
)
def test_configuration_or_provenance_failure_never_loads_or_sends(setup, monkeypatch, change):
    g = setup
    if change == "disabled":
        g.settings.agent_history_mcp_capability_version_id = ""
    elif change == "endpoint":
        g.settings.agent_history_mcp_url = "http://untrusted.example.test/api/v1/agent-history-mcp"
    elif change == "headers":
        g.version.normalized_config_json = {
            **g.config,
            "headers": {"Authorization": "Bearer wrong"},
        }
    elif change == "digest":
        g.version.digest = "0" * 64
    elif change == "blob":
        monkeypatch.setattr(
            integration, "get_artifact_store", lambda: SimpleNamespace(read=lambda _key: b"wrong")
        )
    elif change == "type":
        g.version.normalized_config_json = {**g.config, "command": "arbitrary"}
    else:
        g.runtime.configured = True
    with pytest.raises(DomainError):
        integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert "load" not in g.runtime.calls


def test_snippet_references_keep_the_formal_text_and_source(setup, monkeypatch):
    g = setup
    monkeypatch.setattr(
        conversations,
        "_handle",
        lambda *_args: RuntimeHandle(job_id="runtime", conversation_id="source"),
    )
    monkeypatch.setattr(
        conversations,
        "get_runtime",
        lambda: SimpleNamespace(
            read_event=lambda *_args: RuntimeEvent(
                cursor="event",
                event_type="MESSAGE",
                payload={"source": "assistant", "content": "正式消息正文"},
            )
        ),
    )
    refs = conversations._resolve_message_references(
        g.db,
        g.workspace,
        g.target,
        ({"source_binding_id": g.first.id, "source_event_id": "event"},),
    )
    assert refs == (
        {"event_id": "event", "content": "正式消息正文", "source_binding_id": g.first.id},
    )
    assert integration.ensure(g.db, g.workspace, g.target, refs)
    assert not g.runtime.calls


def test_unconfigured_defaults_do_not_enable_implicit_history():
    settings = Settings(_env_file=None)
    assert not settings.agent_history_mcp_capability_version_id
    assert not settings.agent_history_mcp_url


def test_new_conversation_freezes_mcp_and_secret_before_creation(setup):
    g = setup
    g.target.lifecycle = "PROVISIONING"
    integration.prepare_creation(g.db, g.workspace, g.target, g.refs)
    lease = g.db.scalar(select(AgentHistorySession))
    assert lease.registered_at is None
    assert g.db.scalar(select(AgentConversationCapability)).capability_version_id == g.version.id
    secrets = integration.creation_secrets(g.db, g.target)
    assert history_access.authenticate(g.db, secrets[integration.SECRET_NAME])
    g.runtime.configured = True  # New native creation already applied the frozen MCP configuration.
    assert integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert not g.runtime.calls
    assert g.db.scalar(select(AgentHistorySession)).registered_at is not None


def test_running_dispatch_defers_install_and_never_sends_without_it(setup, monkeypatch):
    g = setup
    sent = []
    monkeypatch.setattr(
        conversations,
        "get_runtime",
        lambda: SimpleNamespace(
            input_readiness=lambda _handle: RuntimeInputReadiness(
                ready=False, execution_status="running"
            ),
            send_message=lambda *_args: sent.append(True),
        ),
    )
    prepared = conversations.PreparedRunningMessage(
        workspace_id="workspace",
        binding_id=g.target.id,
        openhands_conversation_id=g.target.openhands_conversation_id,
        handle=RuntimeHandle(job_id="runtime", conversation_id=g.target.openhands_conversation_id),
        content="继续",
        attachments=(),
        references=g.refs,
        workspace_references=(),
        annotations=(),
        history_reference_ready=False,
    )
    with pytest.raises(DomainError) as caught:
        conversations.dispatch_running_message(prepared)
    assert caught.value.code == "AGENT_HISTORY_CAPABILITY_REQUIRES_IDLE"
    assert not sent


def test_native_config_probe_uses_official_state_and_returns_no_secrets(monkeypatch, tmp_path):
    from flowweave.runtime.openhands import OpenHandsRuntime

    runtime = OpenHandsRuntime(Settings(_env_file=None, workspace_root=tmp_path))
    handle = RuntimeHandle(job_id="runtime", conversation_id="native")
    config = {
        "url": "http://history.example.test/api/v1/agent-history-mcp",
        "transport": "streamable-http",
        "headers": {"Authorization": "**********"},
    }
    monkeypatch.setattr(
        runtime,
        "_conversation_state",
        lambda _handle: {"agent": {"mcp_config": {integration.SERVER_NAME: config}}},
    )
    assert (
        runtime.conversation_mcp_configured(handle, integration.SERVER_NAME, config["url"]) is True
    )
    assert (
        runtime.conversation_mcp_configured(handle, integration.SERVER_NAME, "http://other")
        is False
    )
    config["headers"] = {}
    assert (
        runtime.conversation_mcp_configured(handle, integration.SERVER_NAME, config["url"]) is False
    )


def test_history_registration_migration_sql(monkeypatch):
    import importlib.util
    import io

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "migrations/versions/0161_agent_history_capability.py"
    spec = importlib.util.spec_from_file_location("history_capability_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    monkeypatch.setattr(
        migration,
        "op",
        Operations(
            MigrationContext.configure(
                url="postgresql://", opts={"as_sql": True, "output_buffer": output}
            )
        ),
    )
    migration.upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN runtime_manifest_json JSON DEFAULT '{}' NOT NULL" in sql
    assert "ALTER COLUMN runtime_manifest_json DROP DEFAULT" in sql
    output.seek(0)
    output.truncate()
    migration.downgrade()
    assert output.getvalue().count("DROP COLUMN") == 4


def test_unreachable_or_missing_history_tool_fails_before_native_load(setup):
    g = setup
    g.runtime.probe_ok = False
    with pytest.raises(DomainError) as caught:
        integration.ensure(g.db, g.workspace, g.target, g.refs)
    assert caught.value.code == "AGENT_HISTORY_CAPABILITY_UNAVAILABLE"
    assert not g.runtime.calls
    assert g.db.scalar(select(AgentHistorySession)).registered_at is None


def test_legacy_inline_references_still_project_without_changing_visible_text():
    prompt, _ = conversations.message_payload(
        "用户原文", (), ({"event_id": "old-event", "content": "旧消息引用正文"},)
    )
    assert conversations.project_conversation_references(prompt)[:2] == (
        "用户原文",
        ({"event_id": "old-event", "content": "旧消息引用正文"},),
    )


def test_more_than_ten_whole_references_fail_before_runtime_access(setup, monkeypatch):
    g = setup
    refs = tuple({**g.refs[0], "source_binding_id": f"{index:036d}"} for index in range(11))
    with pytest.raises(DomainError):
        conversations.message_payload("用户请求", (), refs)


def test_metadata_cannot_contain_untrusted_browser_body(setup):
    g = setup
    refs = conversations._resolve_message_references(
        g.db,
        g.workspace,
        g.target,
        (
            {
                "source_binding_id": g.first.id,
                "content": "伪造的正文不能进入模型",
                "source_conversation_id": "wrong",
            },
        ),
    )
    prompt, _ = conversations.message_payload("用户请求", (), refs)
    assert "伪造的正文不能进入模型" not in prompt
    assert refs[0]["source_conversation_id"] == g.first.openhands_conversation_id


def test_failed_bootstrap_discards_committed_history_authority(setup, monkeypatch):
    g = setup
    g.target.lifecycle = "PROVISIONING"
    g.target.workspace_id = "workspace"
    integration.prepare_creation(g.db, g.workspace, g.target, g.refs)
    deleted = []
    db = SimpleNamespace(
        execute=g.db.execute,
        flush=g.db.flush,
        commit=g.db.commit,
        delete=lambda row: deleted.append(row),
    )
    command = object()
    monkeypatch.setattr(
        conversations.agent_workspace_host, "delete_session_attachment_files", lambda *_args: None
    )
    conversations._record_bootstrap_failure(db, g.target, command, integration.unavailable())
    assert deleted == [command, g.target]
    assert g.db.scalar(select(AgentHistorySession)) is None
    assert g.db.scalar(select(AgentConversationCapability)) is None


def test_builtin_mcp_cannot_be_manually_registered_or_replaced(setup):
    g = setup
    with pytest.raises(DomainError) as caught:
        conversations._validated_capabilities(g.db, (g.version.id,))
    assert caught.value.code == "AGENT_HISTORY_CAPABILITY_PLATFORM_MANAGED"
    # Default enablement never authorizes an implicit source.
    assert integration.ensure(g.db, g.workspace, g.target, ())
    assert g.db.scalar(select(AgentHistorySession)) is None
    assert integration.ensure(g.db, g.workspace, g.target, g.refs)
