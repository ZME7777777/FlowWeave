from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flowweave.bootstrap.api import create_app
from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.users.application.ldap import LdapIdentity
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    tenant_user,
)
from flowweave.modules.users.infrastructure.models import UserSession
from flowweave.shared.errors import DomainError


def test_business_api_requires_login(anonymous_client):
    response = anonymous_client.get("/api/v1/node-assets")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_login_me_and_logout(anonymous_client, settings):
    rejected = anonymous_client.post(
        "/api/v1/auth/login",
        json={"username": "flowweave", "password": "incorrect-password"},
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "AUTHENTICATION_FAILED"

    logged_in = anonymous_client.post(
        "/api/v1/auth/login",
        json={
            "username": "flowweave",
            "password": settings.flowweave_admin_password,
        },
    )
    assert logged_in.status_code == 200
    assert logged_in.json() == {
        "id": "00000000-0000-0000-0000-000000000001",
        "username": "flowweave",
        "role": "SUPER_ADMIN",
        "is_super_admin": True,
    }
    assert anonymous_client.get("/api/v1/auth/me").status_code == 200

    logged_out = anonymous_client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 204
    assert anonymous_client.get("/api/v1/auth/me").status_code == 401


def test_ldap_users_are_authorized_before_login_and_revoked_sessions(db_session_factory):
    identity = LdapIdentity(
        external_subject="ldap-employee-1",
        username="employee",
        display_name="Employee One",
        email="employee@example.com",
    )
    from flowweave.modules.users.application import service

    with db_session_factory() as db:
        with pytest.raises(DomainError, match="用户名或密码错误") as rejected:
            service.login_ldap(db, identity)
        assert rejected.value.code == "AUTHENTICATION_FAILED"

        enabled = service.set_ldap_user_enabled(db, identity, enabled=True)
        assert enabled["enabled"] is True
        first_login = service.login_ldap(db, identity)
        assert first_login.principal.username == "employee"

        disabled = service.set_ldap_user_enabled(db, identity, enabled=False)
        assert disabled["enabled"] is False
        assert (
            db.scalar(
                select(UserSession).where(UserSession.user_id == first_login.principal.user_id)
            )
            is None
        )
        with pytest.raises(DomainError, match="用户名或密码错误") as revoked:
            service.login_ldap(db, identity)
        assert revoked.value.code == "AUTHENTICATION_FAILED"


def test_ldap_user_management_requires_super_admin(settings):
    with TestClient(create_app(settings)) as ordinary_client:
        ordinary_login = ordinary_client.post(
            "/api/v1/auth/login",
            json={"username": "user", "password": settings.flowweave_user_password},
        )
        assert ordinary_login.status_code == 200
        forbidden = ordinary_client.get("/api/v1/auth/ldap-users")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"


def test_business_resources_are_shared_between_users(client, user_client):
    admin_directory = client.post("/api/v1/node-directories", json={"name": "共享目录"})
    assert admin_directory.status_code == 201, admin_directory.text

    admin_items = client.get("/api/v1/node-directories")
    user_items = user_client.get("/api/v1/node-directories")
    assert admin_items.status_code == 200, admin_items.text
    assert user_items.status_code == 200, user_items.text
    assert {item["id"] for item in admin_items.json()} == {admin_directory.json()["id"]}
    assert {item["id"] for item in user_items.json()} == {admin_directory.json()["id"]}


def test_credentials_and_flow_runs_are_user_isolated(db_session_factory):
    with db_session_factory() as db:
        with tenant_user(FLOWWEAVE_USER_ID):
            credential = WebsiteCredential(
                name="admin-token",
                target_host="example.com",
                target_path="/",
                include_subdomains=False,
                auth_type="TOKEN",
                encrypted_username=None,
                encrypted_secret=b"encrypted",
                secret_hint="ened",
            )
            run = FlowRun(
                flow_definition_id="shared-flow",
                run_no=1,
                name="admin-run",
            )
            db.add_all((credential, run))
            db.commit()

        with tenant_user(USER_USER_ID):
            assert db.scalars(select(WebsiteCredential)).all() == []
            assert db.scalars(select(FlowRun)).all() == []
            with pytest.raises(RuntimeError, match="Cross-user record creation"):
                db.add(
                    WebsiteCredential(
                        name="forbidden-token",
                        target_host="example.com",
                        target_path="/",
                        include_subdomains=False,
                        auth_type="TOKEN",
                        encrypted_username=None,
                        encrypted_secret=b"encrypted",
                        secret_hint="ened",
                        owner_user_id=FLOWWEAVE_USER_ID,
                    )
                )
                db.flush()
            db.rollback()

        with tenant_user(FLOWWEAVE_USER_ID):
            assert [item.id for item in db.scalars(select(WebsiteCredential))] == [credential.id]
            assert [item.id for item in db.scalars(select(FlowRun))] == [run.id]


def test_shared_flow_allows_each_user_own_run_number_and_node_sessions(client, user_client):
    asset_response = client.post(
        "/api/v1/node-assets",
        json={
            "name": "共享运行节点",
            "description": "",
            "inputs": [],
            "outputs": [],
            "executor": {"startup_prompt": "执行共享流程", "context_prompt": ""},
        },
    )
    assert asset_response.status_code == 201, asset_response.text
    flow_response = client.post(
        "/api/v1/flows",
        json={
            "name": "共享流程 A",
            "description": "所有用户使用同一流程入口",
            "default_entry_key": "shared_node",
            "nodes": [
                {
                    "instance_key": "shared_node",
                    "node_asset_id": asset_response.json()["id"],
                    "alias": "共享节点",
                    "position_x": 0,
                    "position_y": 0,
                    "gates": [],
                }
            ],
            "edges": [],
            "port_mappings": [],
        },
    )
    assert flow_response.status_code == 201, flow_response.text
    flow = flow_response.json()

    user_flows = user_client.get("/api/v1/flows")
    assert user_flows.status_code == 200, user_flows.text
    assert {item["id"] for item in user_flows.json()} == {flow["id"]}

    admin_run_response = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    user_run_response = user_client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": user_client.environment_version_id},
    )
    assert admin_run_response.status_code == 201, admin_run_response.text
    assert user_run_response.status_code == 201, user_run_response.text
    admin_run = admin_run_response.json()
    user_run = user_run_response.json()
    assert admin_run["run_no"] == user_run["run_no"] == 1

    assert [item["id"] for item in client.get("/api/v1/flow-runs").json()] == [admin_run["id"]]
    assert [item["id"] for item in user_client.get("/api/v1/flow-runs").json()] == [user_run["id"]]
    assert user_client.get(f"/api/v1/flow-runs/{admin_run['id']}").status_code == 404
    assert client.get(f"/api/v1/flow-runs/{user_run['id']}").status_code == 404

    node_response = user_client.post(
        f"/api/v1/flow-runs/{user_run['id']}/nodes/shared_node/runs",
        json={
            "agent_preset": {
                "capability_version_ids": [],
                "node_context_enabled": False,
            }
        },
    )
    assert node_response.status_code == 201, node_response.text
    attempt_id = node_response.json()["attempts"][-1]["id"]

    direct_agent = user_client.get("/api/v1/agent-workspaces/default")
    assert direct_agent.status_code == 403, direct_agent.text
    assert direct_agent.json()["error"]["code"] == "AGENT_SESSION_ACCESS_REQUIRED"

    flow_node_sessions = user_client.get(
        f"/api/v1/flow-runs/{user_run['id']}/node-attempts/{attempt_id}/agent-sessions"
    )
    assert flow_node_sessions.status_code == 200, flow_node_sessions.text
    assert flow_node_sessions.json() == {"items": [], "next_cursor": None}
