from datetime import UTC, datetime, timedelta

from flowweave.modules.users.application.security import tenant_bypass
from flowweave.shared.models import EnvironmentVersion, FlowDefinition, FlowRun


def test_version_runs_list_only_top_level_flowruns_and_keep_record_delete_protection(
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
        second_parent = FlowRun(
            flow_definition_id=flow.id,
            run_no=5,
            name="Second FlowRun",
            owner_user_id=admin_id,
            environment_version_id=version_id,
            started_at=now - timedelta(minutes=1),
        )
        db.add_all([child, other_user, other_version, second_parent])
        db.commit()
        parent_id, second_parent_id, flow_id = parent.id, second_parent.id, flow.id

    url = f"/api/v1/terminal-environments/{environment_id}/versions/{version_id}/runs"
    first = client.get(url, params={"page": 1, "page_size": 1})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    assert first.json()["page_size"] == 1
    assert [run["id"] for run in first.json()["items"]] == [second_parent_id]
    assert first.json()["items"][0]["parent_flow_run_id"] is None
    assert first.json()["items"][0]["flow_definition_id"] == flow_id
    assert first.json()["items"][0]["name"] == "Second FlowRun"
    second = client.get(url, params={"page": 2, "page_size": 1})
    assert [run["id"] for run in second.json()["items"]] == [parent_id]
    assert client.get(url, params={"page": 3, "page_size": 1}).json()["items"] == []
    private = user_client.get(url).json()
    assert private["total"] == 2
    assert [run["id"] for run in private["items"]] == [second_parent_id, parent_id]
    listed = client.get(f"/api/v1/terminal-environments/{environment_id}").json()
    detail = next(item for item in listed["versions"] if item["id"] == version_id)
    assert detail["flow_run_reference_count"] == 2
    assert detail["run_reference_count"] == 3
    assert detail["reference_count"] == 3
    blocked = client.delete(f"/api/v1/terminal-environments/{environment_id}/versions/{version_id}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["details"]["run_reference_count"] == 3


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


def test_child_only_version_reference_is_not_a_container_but_still_blocks_deletion(
    client, db_session_factory
):
    with db_session_factory() as db:
        original = db.get(EnvironmentVersion, client.environment_version_id)
        child_version = EnvironmentVersion(
            environment_id=original.environment_id,
            version_no=2,
            state="READY",
            image_reference="test/child-version:2",
            image_digest="sha256:" + "3" * 64,
        )
        flow = FlowDefinition(name="Reference protection", description="")
        db.add_all([child_version, flow])
        db.flush()
        parent = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="Container owner",
            environment_version_id=original.id,
        )
        db.add(parent)
        db.flush()
        db.add(
            FlowRun(
                flow_definition_id=flow.id,
                run_no=2,
                name="Unstarted child record",
                parent_flow_run_id=parent.id,
                environment_version_id=child_version.id,
            )
        )
        db.commit()
        environment_id, version_id = original.environment_id, child_version.id
    path = f"/api/v1/terminal-environments/{environment_id}"
    result = client.get(f"{path}/versions/{version_id}/runs").json()
    assert result["total"] == 0 and result["items"] == []
    version = next(item for item in client.get(path).json()["versions"] if item["id"] == version_id)
    assert version["flow_run_reference_count"] == 0
    assert version["run_reference_count"] == version["reference_count"] == 1
    assert client.delete(f"{path}/versions/{version_id}").status_code == 409
