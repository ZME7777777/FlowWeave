"""Exercise real HTTP authentication, route scopes and ORM filters.

SQLite covers the request/ORM regression without running historical migrations.
PostgreSQL RLS is verified separately against the deployment's read-only probe.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from flowweave.bootstrap import api as bootstrap
from flowweave.bootstrap.settings import Settings
from flowweave.modules.model_providers.application import service as providers
from flowweave.modules.orchestration.application import service as orchestration
from flowweave.modules.runs.infrastructure.models import ArtifactVersion, NodeRun, RunSnapshot
from flowweave.modules.sandboxes.infrastructure.models import (
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    RuntimeGeneration,
)
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.ldap import (
    LdapDirectorySnapshot,
    LdapIdentity,
    LdapOrganization,
)
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, tenant_user
from flowweave.shared.models import (
    FlowDefinition,
    FlowRun,
    LdapAgentSessionOrganizationGrant,
    ModelProvider,
    NodeDirectory,
    ProviderModel,
    User,
    UserSession,
    WebsiteCredential,
)


class AsyncSessionAdapter:
    def __init__(self, session):
        self.sync_session = session

    async def run_sync(self, operation):
        return operation(self.sync_session)

    async def commit(self):
        self.sync_session.commit()

    async def rollback(self):
        self.sync_session.rollback()


@pytest.fixture
def isolated_http(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def sqlite_set_config(connection, _record):
        connection.create_function("set_config", 3, lambda _key, value, _local: value)

    for model in (
        User,
        LdapAgentSessionOrganizationGrant,
        UserSession,
        WebsiteCredential,
        ModelProvider,
        ProviderModel,
        FlowRun,
        RunSnapshot,
        NodeRun,
        ArtifactVersion,
        FlowRunRuntimeSecretReference,
        FlowRunRuntimeAllocation,
        FlowRunRuntime,
        RuntimeGeneration,
        NodeDirectory,
        FlowDefinition,
    ):
        model.__table__.create(engine)

    @asynccontextmanager
    async def session_scope():
        with Session(engine, expire_on_commit=False) as session:
            yield AsyncSessionAdapter(session)

    @asynccontextmanager
    async def uow():
        async with session_scope() as session:
            yield SimpleNamespace(session=session)

    identity = LdapIdentity(
        "directory-subject",
        "employee",
        "Employee",
        None,
        "ldap-org-team",
        ("ldap-org-team", "ldap-org-parent"),
    )
    settings = Settings(
        _env_file=None,
        auth_provider="ldap",
        runtime_adapter="mock",
        ldap_url="ldaps://directory.example.com",
        ldap_base_dn="dc=example,dc=com",
        ldap_search_bind_dn="cn=reader,dc=example,dc=com",
        ldap_search_bind_password="dummy-bind-password",
    )
    container = SimpleNamespace(
        settings=settings,
        database=SimpleNamespace(session=session_scope, uow=uow),
        rate_limiter=SimpleNamespace(allow=AsyncMock(return_value=SimpleNamespace(allowed=True))),
        metrics=Mock(),
        runtime=Mock(),
        artifact_store=Mock(),
        plugin_resolver=Mock(),
        sandbox=Mock(),
        audit_writer=Mock(),
        conversation_hydration_cache=Mock(),
    )
    monkeypatch.setattr(bootstrap, "build_container", lambda *_args, **_kwargs: container)
    monkeypatch.setattr(
        "flowweave.modules.users.presentation.router._ldap_directory",
        lambda _container: SimpleNamespace(authenticate=lambda *_args: identity),
    )
    # Only optional Runtime/reference projections are stubbed; user sessions,
    # CRUD handlers and ownership filtering execute against real ORM records.
    monkeypatch.setattr(providers, "_provider_references", lambda *_args: [])
    monkeypatch.setattr(orchestration.sandboxes, "runtime_readiness_by_flow_run", lambda *_args: {})
    monkeypatch.setattr(orchestration.usage_projection, "for_scope", lambda *_args, **_kw: {})
    with Session(engine) as db:
        users.ensure_builtin_users(db, admin_password="admin-password", user_password="")
        users.set_ldap_user_enabled(db, identity, enabled=True)
        employee_id = db.scalar(select(User.id).where(User.auth_source == "LDAP"))
        for user_id, prefix in ((FLOWWEAVE_USER_ID, "admin"), (employee_id, "employee")):
            with tenant_user(user_id):
                db.add_all(
                    [
                        WebsiteCredential(
                            id=f"{prefix}-credential",
                            name=f"{prefix}-credential",
                            target_host="example.com",
                            auth_type="TOKEN",
                            encrypted_secret=b"secret",
                        ),
                        ModelProvider(
                            id=f"{prefix}-provider",
                            name=f"{prefix}-provider",
                            base_url="https://models.example.com",
                        ),
                        ProviderModel(
                            provider_id=f"{prefix}-provider", model_name=f"{prefix}-model"
                        ),
                    ]
                )
                db.flush()
        with tenant_user(FLOWWEAVE_USER_ID):
            db.add(
                FlowRun(
                    id="shared-run",
                    name="shared-run",
                    flow_definition_id="shared-flow",
                    run_no=1,
                )
            )
        db.commit()
    app = bootstrap.create_app(settings)
    # No lifespan: external services and startup migrations are unnecessary.
    client = TestClient(app)
    yield client, engine, employee_id
    client.close()
    engine.dispose()


def login(client, username):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "admin-password"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ldap_http_lists_and_account_switch_respect_record_ownership(isolated_http):
    client, _engine, employee_id = isolated_http
    for username, prefix in (
        ("employee", "employee"),
        ("flowweave", "admin"),
        ("employee", "employee"),
    ):
        principal = login(client, username)
        if username == "employee":
            assert principal["id"] == employee_id
            assert principal["role"] == "USER"
        for path, suffix in (
            ("website-credentials", "credential"),
            ("model-providers", "provider"),
        ):
            response = client.get(f"/api/v1/{path}")
            assert response.status_code == 200, response.text
            assert [row["id"] for row in response.json()] == [f"{prefix}-{suffix}"]
            if suffix == "provider":
                assert [row["model_name"] for row in response.json()[0]["models"]] == [
                    f"{prefix}-model"
                ]
        runs = client.get("/api/v1/flow-runs")
        assert runs.status_code == 200, runs.text
        assert [row["id"] for row in runs.json()] == ["shared-run"]
        assert client.post("/api/v1/auth/logout").status_code == 204


@pytest.mark.parametrize(
    "path", ["website-credentials/admin-credential", "model-providers/admin-provider"]
)
def test_ldap_cannot_delete_another_users_records_by_id(isolated_http, path):
    client, _engine, _employee_id = isolated_http
    login(client, "employee")
    response = client.delete(f"/api/v1/{path}")
    assert response.status_code == 404, response.text
    login(client, "flowweave")
    response = client.get(f"/api/v1/{path.split('/')[0]}")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_ldap_user_without_private_records_still_sees_shared_flow_run(isolated_http):
    client, engine, employee_id = isolated_http
    with tenant_user(employee_id), Session(engine) as db:
        for model in (WebsiteCredential, ProviderModel, ModelProvider):
            db.execute(delete(model))
        db.commit()
    login(client, "employee")
    for path in ("website-credentials", "model-providers"):
        response = client.get(f"/api/v1/{path}")
        assert response.status_code == 200, response.text
        assert response.json() == []
    runs = client.get("/api/v1/flow-runs")
    assert runs.status_code == 200, runs.text
    assert [row["id"] for row in runs.json()] == ["shared-run"]
    login(client, "flowweave")
    assert len(client.get("/api/v1/website-credentials").json()) == 1
    assert len(client.get("/api/v1/model-providers").json()) == 1


def test_shared_flow_control_plane_and_private_execution_records(isolated_http):
    _client, engine, employee_id = isolated_http
    with tenant_user(employee_id), Session(engine, expire_on_commit=False) as db:
        shared_snapshot = RunSnapshot(
            id="shared-snapshot",
            flow_run_id="shared-run",
            version=1,
            schema_version=2,
            definition_json={},
            definition_hash="shared-definition",
            runtime_manifest_json={},
            runtime_manifest_hash="shared-manifest",
            environment_version_id="shared-environment",
        )
        shared_secret = FlowRunRuntimeSecretReference(
            id="shared-secret",
            encrypted_secret_key=b"encrypted",
            secret_digest="shared-secret-digest",
        )
        db.add_all((shared_snapshot, shared_secret))
        db.flush()
        assert shared_snapshot.owner_user_id == FLOWWEAVE_USER_ID
        assert shared_secret.owner_user_id == employee_id
        shared_allocation = FlowRunRuntimeAllocation(
            id="shared-allocation",
            flow_run_id="shared-run",
            node_attempt_id=None,
            secret_reference_id=shared_secret.id,
            relative_root=".flow-run-runtimes/shared-run",
        )
        db.add(shared_allocation)
        db.flush()
        shared_secret.owner_user_id = FLOWWEAVE_USER_ID
        db.flush()
        shared_runtime = FlowRunRuntime(
            id="shared-runtime",
            flow_run_id="shared-run",
            node_attempt_id=None,
            environment_version_id="shared-environment",
            runtime_image_digest=f"sha256:{'1' * 64}",
            workspace_allocation_id=shared_allocation.id,
            active_generation=1,
        )
        shared_generation = RuntimeGeneration(
            id="shared-generation",
            runtime_session_id=shared_runtime.id,
            generation=1,
            runtime_image_digest=shared_runtime.runtime_image_digest,
            fence_token="shared-fence",
        )
        db.add_all((shared_runtime, shared_generation))
        db.commit()
        assert {
            shared_snapshot.owner_user_id,
            shared_secret.owner_user_id,
            shared_allocation.owner_user_id,
            shared_runtime.owner_user_id,
            shared_generation.owner_user_id,
        } == {FLOWWEAVE_USER_ID}

    private_ids = {}
    for user_id, prefix in ((FLOWWEAVE_USER_ID, "admin"), (employee_id, "employee")):
        with tenant_user(user_id), Session(engine, expire_on_commit=False) as db:
            record = FlowRun(
                id=f"{prefix}-record",
                name=f"{prefix}-record",
                flow_definition_id="shared-flow",
                parent_flow_run_id="shared-run",
                run_no=1,
            )
            db.add(record)
            db.flush()
            snapshot = RunSnapshot(
                id=f"{prefix}-record-snapshot",
                flow_run_id=record.id,
                version=1,
                schema_version=2,
                definition_json={},
                definition_hash=f"{prefix}-definition",
                runtime_manifest_json={},
                runtime_manifest_hash=f"{prefix}-manifest",
                environment_version_id="shared-environment",
            )
            db.add(snapshot)
            node_run = NodeRun(
                id=f"{prefix}-node-run",
                flow_run_id="shared-run",
                flow_node_snapshot_key="shared-node",
                sequence_no=1,
            )
            artifact = ArtifactVersion(
                id=f"{prefix}-artifact",
                flow_run_id="shared-run",
                producer_attempt_id=None,
                consumer_node_key="shared-node",
                field_key="result",
                version_no=1,
                runtime_completion_event_id=None,
                artifact_type="URL",
                storage_key=None,
                uri=f"https://example.com/{prefix}",
                inline_content=None,
                content_hash=f"{prefix}-hash",
                byte_size=0,
                source="HUMAN",
            )
            db.add_all((node_run, artifact))
            db.commit()
            private_ids[user_id] = (record.id, snapshot.id, node_run.id, artifact.id)

    with tenant_user(FLOWWEAVE_USER_ID), Session(engine) as db:
        nested_secret = FlowRunRuntimeSecretReference(
            id="nested-secret",
            encrypted_secret_key=b"nested-encrypted",
            secret_digest="nested-secret-digest",
        )
        nested_allocation = FlowRunRuntimeAllocation(
            id="nested-allocation",
            flow_run_id="admin-record",
            node_attempt_id=None,
            secret_reference_id=nested_secret.id,
            relative_root=".flow-run-runtimes/admin-record",
        )
        nested_runtime = FlowRunRuntime(
            id="nested-runtime",
            flow_run_id="admin-record",
            node_attempt_id=None,
            environment_version_id="shared-environment",
            runtime_image_digest=f"sha256:{'2' * 64}",
            workspace_allocation_id=nested_allocation.id,
            active_generation=1,
        )
        nested_generation = RuntimeGeneration(
            id="nested-generation",
            runtime_session_id=nested_runtime.id,
            generation=1,
            runtime_image_digest=nested_runtime.runtime_image_digest,
            fence_token="nested-fence",
        )
        db.add_all((nested_secret, nested_allocation, nested_runtime, nested_generation))
        db.commit()

    for user_id in (FLOWWEAVE_USER_ID, employee_id):
        with tenant_user(user_id), Session(engine) as db:
            assert db.get(FlowRun, "shared-run") is not None
            assert db.get(RunSnapshot, "shared-snapshot") is not None
            assert db.get(FlowRunRuntimeSecretReference, "shared-secret") is not None
            assert db.get(FlowRunRuntimeAllocation, "shared-allocation") is not None
            assert db.get(FlowRunRuntime, "shared-runtime") is not None
            assert db.get(RuntimeGeneration, "shared-generation") is not None
            visible_records = list(
                db.scalars(select(FlowRun).where(FlowRun.parent_flow_run_id.is_not(None)))
            )
            assert [item.id for item in visible_records] == [private_ids[user_id][0]]
            assert db.get(RunSnapshot, private_ids[user_id][1]) is not None
            assert db.get(NodeRun, private_ids[user_id][2]) is not None
            assert db.get(ArtifactVersion, private_ids[user_id][3]) is not None
            other_user_id = employee_id if user_id == FLOWWEAVE_USER_ID else FLOWWEAVE_USER_ID
            assert db.get(FlowRun, private_ids[other_user_id][0]) is None
            assert db.get(RunSnapshot, private_ids[other_user_id][1]) is None
            assert db.get(NodeRun, private_ids[other_user_id][2]) is None
            assert db.get(ArtifactVersion, private_ids[other_user_id][3]) is None
            nested_models = (
                (FlowRunRuntimeSecretReference, "nested-secret"),
                (FlowRunRuntimeAllocation, "nested-allocation"),
                (FlowRunRuntime, "nested-runtime"),
                (RuntimeGeneration, "nested-generation"),
            )
            for model, item_id in nested_models:
                if user_id == FLOWWEAVE_USER_ID:
                    assert db.get(model, item_id) is not None
                else:
                    assert db.get(model, item_id) is None


def test_ldap_created_private_records_belong_to_ldap_user(isolated_http):
    client, engine, employee_id = isolated_http
    login(client, "employee")
    for path, payload in (
        (
            "website-credentials",
            {
                "name": "new-credential",
                "target_host": "example.org",
                "auth_type": "TOKEN",
                "secret": "new-private-secret",
            },
        ),
        (
            "model-providers",
            {
                "name": "new-provider",
                "base_url": "https://models.example.org",
                "models": [{"model_name": "new-model", "enabled": True, "is_default": True}],
            },
        ),
    ):
        response = client.post(f"/api/v1/{path}", json=payload)
        assert response.status_code == 201, response.text
    with tenant_user(employee_id), Session(engine) as db:
        assert (
            db.scalar(
                select(WebsiteCredential).where(WebsiteCredential.name == "new-credential")
            ).owner_user_id
            == employee_id
        )
        assert (
            db.scalar(
                select(ModelProvider).where(ModelProvider.name == "new-provider")
            ).owner_user_id
            == employee_id
        )
    login(client, "flowweave")
    assert len(client.get("/api/v1/website-credentials").json()) == 1
    assert len(client.get("/api/v1/model-providers").json()) == 1


def test_shared_directory_writes_keep_stable_owner_and_are_visible_to_admin(isolated_http):
    client, engine, _employee_id = isolated_http
    login(client, "employee")
    response = client.post("/api/v1/node-directories", json={"name": "Shared directory"})
    assert response.status_code == 201, response.text
    shared_id = response.json()["id"]
    with Session(engine) as db:
        assert db.get(NodeDirectory, shared_id).owner_user_id == FLOWWEAVE_USER_ID
    login(client, "flowweave")
    assert [row["id"] for row in client.get("/api/v1/node-directories").json()] == [shared_id]


def test_agent_workspace_access_requires_user_or_organization_grant(isolated_http):
    client, engine, employee_id = isolated_http
    principal = login(client, "employee")
    assert principal["can_use_agent_sessions"] is False
    denied = client.get("/api/v1/agent-workspaces/default")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AGENT_SESSION_ACCESS_REQUIRED"

    with Session(engine) as db:
        user = db.get(User, employee_id)
        user.agent_sessions_enabled = True
        db.commit()
    principal = client.get("/api/v1/auth/me").json()
    assert principal["can_use_agent_sessions"] is True

    with Session(engine) as db:
        user = db.get(User, employee_id)
        user.agent_sessions_enabled = False
        db.add(
            LdapAgentSessionOrganizationGrant(
                organization_id="ldap-org-parent",
                created_by_user_id=FLOWWEAVE_USER_ID,
            )
        )
        db.commit()
    principal = client.get("/api/v1/auth/me").json()
    assert principal["can_use_agent_sessions"] is True

    login(client, "flowweave")
    assert client.get("/api/v1/auth/me").json()["can_use_agent_sessions"] is True


def test_ldap_directory_projects_direct_and_inherited_agent_access(isolated_http):
    _client, engine, _employee_id = isolated_http
    identity = LdapIdentity(
        "directory-subject",
        "employee",
        "Employee",
        None,
        "ldap-org-team",
        ("ldap-org-team", "ldap-org-parent"),
    )
    snapshot = LdapDirectorySnapshot(
        users=[identity],
        organizations=[
            LdapOrganization("ldap-org-parent", None, "Parent"),
            LdapOrganization("ldap-org-team", "ldap-org-parent", "Team"),
        ],
    )
    with Session(engine) as db:
        initial = users.ldap_directory(db, snapshot)
        assert initial["users"][0]["agent_session_access"] is False
        users.set_ldap_organization_agent_session_access(
            db,
            "ldap-org-parent",
            enabled=True,
            actor_user_id=FLOWWEAVE_USER_ID,
            identities=snapshot.users,
        )
        inherited = users.ldap_directory(db, snapshot)
        assert inherited["organizations"][0]["agent_session_direct_access"] is True
        assert inherited["organizations"][1]["agent_session_access"] is True
        assert inherited["users"][0]["agent_session_inherited_access"] is True
        assert inherited["users"][0]["agent_session_access"] is True


def test_admin_manages_agent_access_by_ldap_user_and_organization(isolated_http, monkeypatch):
    client, _engine, _employee_id = isolated_http
    identity = LdapIdentity(
        "directory-subject",
        "employee",
        "Employee",
        None,
        "ldap-org-team",
        ("ldap-org-team", "ldap-org-parent"),
    )
    snapshot = LdapDirectorySnapshot(
        users=[identity],
        organizations=[
            LdapOrganization("ldap-org-parent", None, "Parent"),
            LdapOrganization("ldap-org-team", "ldap-org-parent", "Team"),
        ],
    )
    monkeypatch.setattr(
        "flowweave.modules.users.presentation.router._ldap_directory",
        lambda _container: SimpleNamespace(directory_snapshot=lambda: snapshot),
    )
    login(client, "flowweave")

    direct = client.put(
        "/api/v1/auth/ldap-users/agent-session-access",
        json={"external_subject": identity.external_subject, "enabled": True},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["agent_session_direct_access"] is True

    organization = client.put(
        "/api/v1/auth/ldap-organizations/agent-session-access",
        json={"organization_id": "ldap-org-parent", "enabled": True},
    )
    assert organization.status_code == 204, organization.text
    direct = client.put(
        "/api/v1/auth/ldap-users/agent-session-access",
        json={"external_subject": identity.external_subject, "enabled": False},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["agent_session_direct_access"] is False
    assert direct.json()["agent_session_inherited_access"] is True

    directory = client.get("/api/v1/auth/ldap-users")
    assert directory.status_code == 200, directory.text
    assert directory.json()["organizations"][0]["agent_session_direct_access"] is True
    assert directory.json()["users"][0]["agent_session_access"] is True
