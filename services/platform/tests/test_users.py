from __future__ import annotations

from sqlalchemy import select

from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspacePreference,
)
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    tenant_user,
)


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


def test_business_resources_are_shared_between_users(client, user_client):
    admin_directory = client.post(
        "/api/v1/node-directories", json={"name": "共享目录"}
    )
    assert admin_directory.status_code == 201, admin_directory.text

    admin_items = client.get("/api/v1/node-directories")
    user_items = user_client.get("/api/v1/node-directories")
    assert admin_items.status_code == 200, admin_items.text
    assert user_items.status_code == 200, user_items.text
    assert {item["id"] for item in admin_items.json()} == {admin_directory.json()["id"]}
    assert {item["id"] for item in user_items.json()} == {admin_directory.json()["id"]}


def test_independent_agent_preferences_remain_user_isolated(db_session_factory):
    workspace_id = "00000000-0000-0000-0000-000000000099"
    with db_session_factory() as db:
        with tenant_user(FLOWWEAVE_USER_ID):
            db.add(
                AgentWorkspacePreference(
                    workspace_id=workspace_id,
                    default_model_provider_id="provider-admin",
                )
            )
            db.commit()

        with tenant_user(USER_USER_ID):
            assert db.scalar(
                select(AgentWorkspacePreference).where(
                    AgentWorkspacePreference.workspace_id == workspace_id
                )
            ) is None
            db.add(
                AgentWorkspacePreference(
                    workspace_id=workspace_id,
                    default_model_provider_id="provider-user",
                )
            )
            db.commit()

        with tenant_user(FLOWWEAVE_USER_ID):
            preference = db.scalar(
                select(AgentWorkspacePreference).where(
                    AgentWorkspacePreference.workspace_id == workspace_id
                )
            )
            assert preference is not None
            assert preference.default_model_provider_id == "provider-admin"
