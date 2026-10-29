from datetime import UTC, datetime, timedelta

from flowweave.modules.users.application.security import tenant_bypass
from flowweave.shared.models import EnvironmentVersion, FlowDefinition, FlowRun


def test_version_runs_include_children_and_paginate_with_user_isolation(
    client, user_client, db_session_factory
):
    admin_id = client.get("/api/v1/auth/me").json()["id"]
    user_id = user_client.get("/api/v1/auth/me").json()["id"]
    version_id = client.environment_version_id
    with tenant_bypass(), db_session_factory() as db:
        version = db.get(EnvironmentVersion, version_id)
        environment_id = version.environment_id
        flow = FlowDefinition(name="Version reference flow", description="")
        db.add(flow)
        db.flush()
        now = datetime.now(UTC)
        parent = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="Parent",
            owner_user_id=admin_id,
            environment_version_id=version_id,
            started_at=now - timedelta(minutes=3),
        )
        db.add(parent)
        db.flush()
        child = FlowRun(
            flow_definition_id=flow.id,
            run_no=2,
            name="Child",
            run_mode="AUTOMATIC",
            parent_flow_run_id=parent.id,
            owner_user_id=admin_id,
            environment_version_id=version_id,
            started_at=now - timedelta(minutes=2),
        )
        other_user = FlowRun(
            flow_definition_id=flow.id,
            run_no=3,
            name="Private other user",
            parent_flow_run_id=parent.id,
            owner_user_id=user_id,
            environment_version_id=version_id,
            started_at=now,
        )
        other_version = FlowRun(
            flow_definition_id=flow.id,
            run_no=4,
            name="Another environment",
            owner_user_id=admin_id,
            environment_version_id=user_client.environment_version_id,
        )
        db.add_all([child, other_user, other_version])
        db.commit()
        parent_id, child_id, other_user_id = parent.id, child.id, other_user.id

    url = f"/api/v1/terminal-environments/{environment_id}/versions/{version_id}/runs"
    first = client.get(url, params={"page": 1, "page_size": 1})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    assert first.json()["page_size"] == 1
    assert [run["id"] for run in first.json()["items"]] == [child_id]
    assert first.json()["items"][0]["parent_flow_run_id"] == parent_id
    assert first.json()["items"][0]["run_mode"] == "AUTOMATIC"
    second = client.get(url, params={"page": 2, "page_size": 1})
    assert [run["id"] for run in second.json()["items"]] == [parent_id]
    assert client.get(url, params={"page": 3, "page_size": 1}).json()["items"] == []
    private = user_client.get(url).json()
    assert private["total"] == 2
    assert [run["id"] for run in private["items"]] == [other_user_id, parent_id]


def test_version_runs_validate_parent_pagination_and_authentication(
    client, user_client, anonymous_client, db_session_factory
):
    with db_session_factory() as db:
        version = db.get(EnvironmentVersion, client.environment_version_id)
        environment_id = version.environment_id
    path = f"/api/v1/terminal-environments/{environment_id}/versions"
    url = f"{path}/{client.environment_version_id}/runs"
    assert client.get(url).json()["total"] == 0
    assert client.get(url, params={"page": 0}).status_code == 422
    assert client.get(url, params={"page_size": 101}).status_code == 422
    assert client.get(f"{path}/missing/runs").status_code == 404
    assert client.get(url.replace(environment_id, "missing")).status_code == 404
    with db_session_factory() as db:
        other = db.get(EnvironmentVersion, user_client.environment_version_id)
        other_environment_id = other.environment_id
    assert client.get(url.replace(environment_id, other_environment_id)).status_code == 404
    assert anonymous_client.get(url).status_code == 401
