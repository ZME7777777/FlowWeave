import base64
import io
import zipfile

from sqlalchemy import select

from flowweave.shared.models import (
    CapabilityCollection,
    CapabilityCollectionItem,
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


def _revised_skill_zip() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("test-skill/SKILL.md", "# Test Skill\nUpdated instructions.\n")
    return base64.b64encode(buffer.getvalue()).decode()


def test_capability_collection_is_a_logical_skill_selection_template(
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

    with db_session_factory() as db:
        assert db.scalar(select(CapabilityCollection)).id == collection["id"]
        assert db.scalar(select(CapabilityCollectionItem)).collection_id == collection["id"]

    updated = client.put(
        f"/api/v1/capability-collections/{collection['id']}",
        json={
            **_collection_payload(skill_capability["capability_id"]),
            "name": "产品分析组合（更新）",
            "row_version": collection["row_version"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "产品分析组合（更新）"
    assert [member["id"] for member in updated.json()["members"]] == [
        skill_capability["capability_id"]
    ]


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


def test_deleting_skill_detaches_logical_collection_shortcut(client, skill_capability):
    created = client.post(
        "/api/v1/capability-collections",
        json=_collection_payload(skill_capability["capability_id"]),
    )
    assert created.status_code == 201, created.text

    deleted = client.delete(f"/api/v1/capabilities/{skill_capability['capability_id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/capability-collections").json() == []


def test_reimported_skill_replaces_collection_member_with_latest_version(client, skill_capability):
    created = client.post(
        "/api/v1/capability-collections",
        json=_collection_payload(skill_capability["capability_id"]),
    )
    assert created.status_code == 201, created.text

    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "test-skill.zip",
            "content_base64": _revised_skill_zip(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports", json={"import_token": validated.json()["import_token"]}
    )
    assert committed.status_code == 201, committed.text
    latest_id = committed.json()["capabilities"][0]["capability_id"]
    assert latest_id != skill_capability["capability_id"]

    collection = client.get("/api/v1/capability-collections").json()[0]
    assert collection["row_version"] == created.json()["row_version"] + 1
    assert [member["id"] for member in collection["members"]] == [latest_id]
    assert collection["members"][0]["is_latest"] is True


def test_legacy_skill_collection_routes_are_removed(client):
    assert client.get("/api/v1/skill-collections").status_code == 404
