"""Draft definitions remain editable while launch requires a start node."""

from test_api import create_asset, create_flow


def test_empty_flow_draft_can_be_saved_and_loaded(client):
    response = client.post("/api/v1/flows", json={"name": "Empty draft"})
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["nodes"] == []
    loaded = client.get(f"/api/v1/flows/{draft['id']}")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["nodes"] == []


def test_empty_flow_rejects_manual_and_automatic_launch(client):
    saved = client.post("/api/v1/flows", json={"name": "Unstartable draft", "nodes": []})
    assert saved.status_code == 201, saved.text
    flow_id = saved.json()["id"]
    for suffix in ("runs", "automatic-runs"):
        payload = {"environment_version_id": client.environment_version_id}
        if suffix == "automatic-runs":
            payload["start_node_key"] = "first"
        response = client.post(
            f"/api/v1/flows/{flow_id}/{suffix}",
            json=payload,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "FLOW_START_NODE_MISSING"
    assert client.get("/api/v1/flow-runs").json() == []


def test_populated_flow_still_starts(client, skill_capability):
    asset = create_asset(client, skill_capability, "Startable draft node")
    flow = create_flow(client, asset["id"])
    response = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    assert response.status_code == 201, response.text
    assert response.json()["flow_definition_id"] == flow["id"]
