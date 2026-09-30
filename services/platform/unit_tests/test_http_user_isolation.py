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
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.ldap import LdapIdentity
from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, tenant_user
from flowweave.shared.models import (
    FlowDefinition,
    FlowRun,
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
        UserSession,
        WebsiteCredential,
        ModelProvider,
        ProviderModel,
        FlowRun,
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

    identity = LdapIdentity("directory-subject", "employee", "Employee", None)
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
                        FlowRun(
                            id=f"{prefix}-run",
                            name=f"{prefix}-run",
                            flow_definition_id="shared-flow",
                            run_no=1 if prefix == "admin" else 2,
                        ),
                    ]
                )
                db.flush()
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
            ("flow-runs", "run"),
        ):
            response = client.get(f"/api/v1/{path}")
            assert response.status_code == 200, response.text
            assert [row["id"] for row in response.json()] == [f"{prefix}-{suffix}"]
            if suffix == "provider":
                assert [row["model_name"] for row in response.json()[0]["models"]] == [
                    f"{prefix}-model"
                ]
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


def test_ldap_user_without_private_records_gets_empty_http_lists(isolated_http):
    client, engine, employee_id = isolated_http
    with tenant_user(employee_id), Session(engine) as db:
        for model in (WebsiteCredential, ProviderModel, ModelProvider, FlowRun):
            db.execute(delete(model))
        db.commit()
    login(client, "employee")
    for path in ("website-credentials", "model-providers", "flow-runs"):
        response = client.get(f"/api/v1/{path}")
        assert response.status_code == 200, response.text
        assert response.json() == []
    login(client, "flowweave")
    assert len(client.get("/api/v1/website-credentials").json()) == 1
    assert len(client.get("/api/v1/model-providers").json()) == 1


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
