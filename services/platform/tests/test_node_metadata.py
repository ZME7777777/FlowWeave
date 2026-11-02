import pytest


def node_payload():
    return {
        "name": "固定名称节点",
        "description": "创建时确定的节点用途",
        "inputs": [{"field_key": "source", "display_name": "来源", "data_type": "URL"}],
        "outputs": [{"field_key": "report", "display_name": "报告", "data_type": "FILE"}],
        "executor": {"startup_prompt": "读取来源", "context_prompt": "保留证据"},
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "尝试改名"},
        {"description": "尝试修改说明"},
        {"name": "尝试改名", "description": "尝试修改说明"},
    ],
)
def test_node_metadata_update_is_rejected_without_partial_changes(client, changes):
    payload = node_payload()
    created = client.post("/api/v1/node-assets", json=payload)
    assert created.status_code == 201, created.text
    original = created.json()
    response = client.put(
        f"/api/v1/node-assets/{original['id']}",
        json={
            **payload,
            "row_version": original["row_version"],
            "inputs": [],
            "executor": {"startup_prompt": "不应保存的改动"},
            **changes,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "NODE_ASSET_METADATA_IMMUTABLE"
    assert set(response.json()["error"]["details"]["fields"]) == set(changes)
    assert client.get(f"/api/v1/node-assets/{original['id']}").json() == original


def test_node_content_can_still_be_saved_with_unchanged_metadata_and_version_checks(client):
    payload = node_payload()
    original = client.post("/api/v1/node-assets", json=payload).json()
    updated_payload = {
        **payload,
        "row_version": original["row_version"],
        "inputs": [{"field_key": "source", "display_name": "资料来源", "data_type": "FILE"}],
        "executor": {"startup_prompt": "执行修订步骤", "context_prompt": "新的约束"},
    }
    response = client.put(f"/api/v1/node-assets/{original['id']}", json=updated_payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["name"] == original["name"]
    assert saved["description"] == original["description"]
    assert saved["row_version"] == original["row_version"] + 1
    assert saved["executor"]["startup_prompt"] == "执行修订步骤"
    assert saved["executor"]["context_prompt"] == "新的约束"
    assert saved["inputs"][0]["display_name"] == "资料来源"
    assert saved["inputs"][0]["field_key"] == "source"
    assert saved["inputs"][0]["data_type"] == "FILE"
    rejected = client.put(f"/api/v1/node-assets/{original['id']}", json=updated_payload)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "VERSION_CONFLICT"
    assert client.get(f"/api/v1/node-assets/{original['id']}").json() == saved
