from sqlalchemy import select

from flowweave.shared.models import (
    CapabilityCollection,
    CapabilityCollectionItem,
    NodeCapabilityRef,
)


def _collection_payload(*capability_ids: str, row_version: int | None = None) -> dict:
    payload = {
        "name": "产品分析组合",
        "category": "产品",
        "description": "节点创建时批量选择，运行时仍使用真实能力版本。",
        "capability_ids": list(capability_ids),
    }
    if row_version is not None:
        payload["row_version"] = row_version
    return payload


def test_capability_collection_expands_to_real_node_capability_refs(
    client, skill_capability, db_session_factory
):
    created = client.post(
        "/api/v1/capability-collections",
        json=_collection_payload(skill_capability["capability_id"]),
    )
    assert created.status_code == 201, created.text
    collection = created.json()
    assert collection["category"] == "产品"
    assert [member["id"] for member in collection["members"]] == [skill_capability["capability_id"]]

    node = client.post(
        "/api/v1/node-assets",
        json={
            "name": "组合展开节点",
            "executor": {},
            "capabilities": [{"capability_id": member["id"]} for member in collection["members"]],
        },
    )
    assert node.status_code == 201, node.text
    node_skills = [
        item for item in node.json()["capabilities"] if item["capability_type"] == "SKILL"
    ]
    assert [item["capability_id"] for item in node_skills] == [skill_capability["capability_id"]]

    with db_session_factory() as db:
        assert db.scalar(select(CapabilityCollection)).id == collection["id"]
        assert db.scalar(select(CapabilityCollectionItem)).collection_id == collection["id"]
        refs = db.scalars(select(NodeCapabilityRef)).all()
        skill_refs = [item for item in refs if item.capability_type == "SKILL"]
        assert len(skill_refs) == 1
        assert skill_refs[0].normalized_config["capability_id"] == skill_capability["capability_id"]
        assert "collection_id" not in skill_refs[0].normalized_config

    updated = client.put(
        f"/api/v1/capability-collections/{collection['id']}",
        json={
            **_collection_payload(skill_capability["capability_id"]),
            "name": "产品分析组合（更新）",
            "row_version": collection["row_version"],
        },
    )
    assert updated.status_code == 200, updated.text
    persisted = client.get(f"/api/v1/node-assets/{node.json()['id']}").json()
    persisted_skills = [
        item for item in persisted["capabilities"] if item["capability_type"] == "SKILL"
    ]
    assert persisted_skills[0]["capability_id"] == skill_capability["capability_id"]


def test_skill_collection_rejects_non_skill_members(client, skill_capability):
    remote = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "mcp.json",
            "content_base64": (
                "eyJtY3BTZXJ2ZXJzIjp7ImRvY3MiOnsidXJsIjoiaHR0cHM6Ly9tY3AuZXhhbXBsZS5jb20ifX19"
            ),
        },
    )
    assert remote.status_code == 200, remote.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": remote.json()["import_token"]},
    ).json()
    rejected = client.post(
        "/api/v1/capability-collections",
        json=_collection_payload(
            skill_capability["capability_id"],
            committed["capabilities"][0]["capability_id"],
        ),
    )
    assert rejected.status_code == 422, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "SKILL_COLLECTION_TYPE_REQUIRED"
    assert error["details"] == {"capability_ids": [committed["capabilities"][0]["capability_id"]]}
    assert client.get("/api/v1/capability-collections").json() == []


def test_legacy_skill_collection_routes_are_removed(client):
    assert client.get("/api/v1/skill-collections").status_code == 404
