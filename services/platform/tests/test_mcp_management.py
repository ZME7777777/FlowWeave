import base64
import json

import pytest

from flowweave.modules.catalog.application import capability_imports
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import settings_context


def document(key, url):
    return json.dumps({"mcpServers": {key: {"url": url, "transport": "streamable-http"}}})


def validate(client, content):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "mcp.json",
            "content_base64": base64.b64encode(content.encode()).decode(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["import_token"]


def publish(client, content):
    response = client.post(
        "/api/v1/capability-imports", json={"import_token": validate(client, content)}
    )
    assert response.status_code == 201, response.text
    return response.json()["capabilities"][0]["capability_id"]


def test_builtin_mcp_projection_delete_edit_and_reimport_are_protected(client):
    key = "flowweave-reference-history"
    content = document(key, "https://history.example.test/mcp")
    builtin = publish(client, content)
    ordinary = publish(client, document("ordinary", "https://ordinary.example.test/mcp"))
    listing = client.get("/api/v1/capability-directory", params={"capability_type": "MCP"}).json()
    group = next(item for item in listing["items"] if item["latest"]["id"] == builtin)
    assert group["latest"]["is_builtin"] is True
    assert all(item["is_builtin"] for item in group["versions"])
    update = client.put(
        f"/api/v1/capabilities/{builtin}/mcp-source",
        json={
            "content": document(key, "https://changed.example.test/mcp"),
            "mcp_scripts": [],
        },
    )
    assert update.status_code == 403
    assert update.json()["error"]["code"] == "BUILTIN_CAPABILITY"
    single = client.delete(f"/api/v1/capabilities/{builtin}")
    assert single.status_code == 409
    batch = client.request("DELETE", "/api/v1/capabilities", json={"ids": [builtin, ordinary]})
    assert batch.status_code == 200, batch.text
    assert batch.json()["deleted_ids"] == [ordinary]
    assert batch.json()["blocked"][0]["relation"] == "BUILTIN_CAPABILITY"
    token = validate(client, document(key, "https://replacement.example.test/mcp"))
    replacement = client.post("/api/v1/capability-imports", json={"import_token": token})
    assert replacement.status_code == 403
    source = client.get(f"/api/v1/capabilities/{builtin}/mcp-source")
    assert json.loads(source.json()["content"]) == json.loads(content)


def test_mcp_versions_preserve_history_and_reject_stale_base(client):
    first_content = document("versioned", "https://first.example.test/mcp")
    first = publish(client, first_content)
    revised = client.put(
        f"/api/v1/capabilities/{first}/mcp-source",
        json={
            "content": document("versioned", "https://second.example.test/mcp"),
            "mcp_scripts": [],
        },
    )
    assert revised.status_code == 200, revised.text
    second = revised.json()
    assert second["revision_number"] == 2 and second["is_latest"]
    history = client.get(f"/api/v1/capabilities/{first}/mcp-source").json()
    assert json.loads(history["content"]) == json.loads(first_content)
    stale = client.put(
        f"/api/v1/capabilities/{first}/mcp-source",
        json={
            "content": document("versioned", "https://stale.example.test/mcp"),
            "mcp_scripts": [],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CAPABILITY_SOURCE_CONFLICT"
    duplicate = client.put(
        f"/api/v1/capabilities/{second['id']}/mcp-source",
        json={
            "content": first_content,
            "mcp_scripts": [],
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "CAPABILITY_SOURCE_UNCHANGED"
    versions = client.get("/api/v1/capability-directory", params={"capability_type": "MCP"}).json()[
        "items"
    ][0]
    assert versions["latest"]["id"] == second["id"]
    assert [item["revision_number"] for item in versions["versions"]] == [2, 1]


def test_mcp_checks_latest_again_after_source_preparation(client, db_session_factory, container):
    identity = publish(client, document("race", "https://first.example.test/mcp"))
    with settings_context(container.settings), artifact_store_context(container.artifact_store):
        with db_session_factory() as db:
            plan = capability_imports.prepare_mcp_update(
                db, identity, document("race", "https://late.example.test/mcp"), []
            )
            db.rollback()
        newer = client.put(
            f"/api/v1/capabilities/{identity}/mcp-source",
            json={
                "content": document("race", "https://newer.example.test/mcp"),
                "mcp_scripts": [],
            },
        )
        assert newer.status_code == 200, newer.text
        stored = capability_imports.store_skill_update_source(plan)
        final_key = capability_imports.finalize_skill_update_source(stored)
        with db_session_factory() as db:
            with pytest.raises(DomainError) as error:
                capability_imports.confirm_mcp_update(db, stored, final_key)
            assert error.value.code == "CAPABILITY_SOURCE_CONFLICT"
            db.rollback()
