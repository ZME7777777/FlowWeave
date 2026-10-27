from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from flowweave.shared.models import CapabilityBlob, CapabilityPackage, CapabilityVersion


@pytest.fixture
def repository_records(client, db_session_factory):
    owner = client.get("/api/v1/auth/me").json()["id"]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with db_session_factory() as db:
        for index in range(25):
            package = CapabilityPackage(
                owner_user_id=owner,
                capability_type="SKILL",
                capability_key=f"skill-{24 - index:02}",
                display_name=f"Skill {index}",
                description="literal_% description" if index == 0 else "repository fixture",
            )
            blob = CapabilityBlob(
                content_hash=uuid4().hex * 2, storage_key=f"test://{uuid4()}", byte_size=100 + index
            )
            db.add_all([package, blob])
            db.flush()
            for revision in range(1, 4 if index == 0 else 2):
                db.add(
                    CapabilityVersion(
                        owner_user_id=owner,
                        package_id=package.id,
                        blob_id=blob.id,
                        version_no=revision,
                        digest=uuid4().hex * 2,
                        source_filename=f"skill-{index}.zip",
                        created_at=start + timedelta(days=index, hours=revision),
                        normalized_config_json={},
                        state="RETIRED" if revision == 3 else "PUBLISHED",
                    )
                )
        db.commit()


def test_global_sort_precedes_pagination_and_keeps_complete_lineages(client, repository_records):
    first = client.get("/api/v1/capability-directory", params={"capability_type": "SKILL"}).json()
    assert first["total"] == 25
    assert first["counts"]["SKILL"] == 25
    assert len(first["items"]) == 20
    assert first["items"][0]["latest"]["capability_key"] == "skill-00"
    second = client.get(
        "/api/v1/capability-directory",
        params={
            "capability_type": "SKILL",
            "page": 2,
        },
    ).json()
    assert len(second["items"]) == 5
    assert second["items"][-1]["latest"]["revision_number"] == 2
    assert len(second["items"][-1]["versions"]) == 2
    for direction, expected in [("asc", range(7)), ("desc", range(24, 17, -1))]:
        result = client.get(
            "/api/v1/capability-directory",
            params={
                "capability_type": "SKILL",
                "page_size": 7,
                "sort": "name",
                "direction": direction,
            },
        ).json()
        assert [item["latest"]["capability_key"] for item in result["items"]] == [
            f"skill-{index:02}" for index in expected
        ]
    sized = client.get(
        "/api/v1/capability-directory",
        params={
            "capability_type": "SKILL",
            "sort": "byte_size",
            "direction": "asc",
            "page_size": 1,
        },
    ).json()
    assert sized["items"][0]["latest"]["byte_size"] == 100
    literal = client.get(
        "/api/v1/capability-directory",
        params={
            "capability_type": "SKILL",
            "q": "_%",
        },
    ).json()
    assert literal["total"] == 1
    assert literal["items"][0]["latest"]["capability_key"] == "skill-24"
    assert (
        client.get(
            "/api/v1/capability-directory",
            params={
                "capability_type": "MCP",
            },
        ).json()["items"]
        == []
    )
    assert (
        client.get(
            "/api/v1/capability-directory",
            params={
                "capability_type": "SKILL",
                "page": 50,
            },
        ).json()["items"]
        == []
    )


def test_directory_stable_ties_and_collection_global_sort(client, repository_records):
    versions = client.get("/api/v1/capabilities").json()
    latest = [
        item["id"] for item in versions if item["capability_type"] == "SKILL" and item["is_latest"]
    ]
    for index in range(5):
        response = client.post(
            "/api/v1/capability-collections",
            json={
                "name": f"collection-{4 - index}",
                "category": "test",
                "description": "",
                "capability_ids": latest[: index + 1],
            },
        )
        assert response.status_code == 201, response.text
    pages = [
        client.get(
            "/api/v1/capability-collection-directory",
            params={
                "page_size": 2,
                "page": page,
                "sort": "name",
                "direction": "asc",
            },
        ).json()
        for page in (1, 2)
    ]
    assert pages[0]["total"] == 5
    assert [item["name"] for page in pages for item in page["items"]] == [
        f"collection-{index}" for index in range(4)
    ]
    count = client.get(
        "/api/v1/capability-collection-directory",
        params={
            "page_size": 1,
            "sort": "member_count",
            "direction": "desc",
        },
    ).json()
    assert len(count["items"][0]["members"]) == 5
    pages = [
        client.get(
            "/api/v1/capability-directory",
            params={
                "capability_type": "SKILL",
                "sort": "revision_number",
                "direction": "asc",
                "page_size": 10,
                "page": page,
            },
        ).json()["items"]
        for page in (1, 2, 3)
    ]
    ids = [item["id"] for page in pages for item in page]
    assert len(set(ids)) == len(ids) == 25
    repeat = client.get(
        "/api/v1/capability-directory",
        params={
            "capability_type": "SKILL",
            "sort": "revision_number",
            "direction": "asc",
            "page_size": 10,
        },
    ).json()["items"]
    assert [item["id"] for item in repeat] == ids[:10]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 101},
        {"sort": "unknown"},
        {"direction": "sql"},
        {"capability_type": "PRIVATE_TYPE"},
        {"q": "x" * 201},
    ],
)
def test_directory_rejects_invalid_page_and_order(client, params):
    response = client.get(
        "/api/v1/capability-directory", params={"capability_type": "SKILL", **params}
    )
    assert response.status_code == 422
