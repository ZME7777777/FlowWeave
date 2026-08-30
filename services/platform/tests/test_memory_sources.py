from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from flowweave.modules.catalog.application import memory_sources
from flowweave.runtime.request import frozen_memory_policy
from flowweave.shared.models import (
    CapabilityBlob,
    CapabilityPackage,
    CapabilityVersion,
    FlowDefinition,
    FlowRun,
    MemorySource,
    MemorySourceVersion,
    MemorySourceVersionReference,
    RunSnapshot,
)


def _payload(content: str = "# Project memory\r\n\r\nStable fact.\r\n") -> dict[str, str]:
    return {
        "source_key": "project-guidance",
        "display_name": "Project guidance",
        "owner_id": "user-123",
        "scope": "PROJECT",
        "scope_key": "project-456",
        "content": content,
    }


def test_frozen_memory_policy_isolated_by_runtime_scope():
    reference = {"reference_id": str(uuid4()), "digest": "a" * 64}
    node = {
        "runtime_agent_spec": {
            "memory_policy": {
                "capability_key": "attempt-memory",
                "runtime_config": {
                    "name": "attempt-memory",
                    "enabled": True,
                    "scopes": ["ATTEMPT"],
                    "source_refs": [reference],
                    "retention_days": 30,
                    "require_review": True,
                    "sensitive_data_scan": True,
                    "replay_mode": "FROZEN",
                },
            }
        }
    }

    assert frozen_memory_policy(node, runtime_scope="ATTEMPT") == (True, [reference])
    assert frozen_memory_policy(node, runtime_scope="CONVERSATION") == (False, [])


def _govern_and_activate(client, source: dict[str, object], *, retention_days: int = 30):
    version = source["latest_version"]
    assert isinstance(version, dict)
    source_id = str(source["id"])
    version_id = str(version["id"])
    reviewed = client.post(
        f"/api/v1/memory-sources/{source_id}/versions/{version_id}/review",
        json={"expected_governance_version": 1, "decision": "APPROVE"},
        headers={"X-Actor-ID": "reviewer-456"},
    )
    assert reviewed.status_code == 200, reviewed.text
    scanned = client.post(
        f"/api/v1/memory-sources/{source_id}/versions/{version_id}/scan",
        json={"expected_governance_version": reviewed.json()["governance_version"]},
    )
    assert scanned.status_code == 200, scanned.text
    activated = client.post(
        f"/api/v1/memory-sources/{source_id}/versions/{version_id}/activate",
        json={
            "expected_governance_version": scanned.json()["governance_version"],
            "retention_days": retention_days,
        },
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def _create_memory_policy_reference(db, active: dict[str, object]) -> str:
    identity = uuid4().hex
    blob = CapabilityBlob(
        content_hash=hashlib.sha256(f"blob:{identity}".encode()).hexdigest(),
        storage_key=f"tests/memory-policy/{identity}",
        byte_size=2,
        media_type="application/json",
    )
    package = CapabilityPackage(
        capability_type="MEMORY_POLICY",
        capability_key=f"memory-retention-{identity}",
        display_name="Memory retention test policy",
    )
    db.add_all([blob, package])
    db.flush()
    version = CapabilityVersion(
        package_id=package.id,
        blob_id=blob.id,
        version_no=1,
        digest=hashlib.sha256(f"version:{identity}".encode()).hexdigest(),
        normalized_config_json={
            "name": package.capability_key,
            "description": "",
            "enabled": True,
            "scopes": ["PROJECT"],
            "source_refs": [{"reference_id": active["id"], "digest": active["digest"]}],
            "retention_days": 1,
            "require_review": True,
            "sensitive_data_scan": True,
            "replay_mode": "FROZEN",
        },
        source_filename=f"{package.capability_key}.json",
    )
    db.add(version)
    db.flush()
    reference = MemorySourceVersionReference(
        memory_source_version_id=str(active["id"]),
        reference_kind="POLICY_VERSION",
        reference_id=version.id,
    )
    db.add(reference)
    db.commit()
    return reference.id


def test_memory_source_freezes_canonical_content_and_redacts_reads(client, db_session_factory):
    response = client.post("/api/v1/memory-sources", json=_payload())
    assert response.status_code == 201, response.text
    source = response.json()
    version = source["versions"][0]
    canonical = "# Project memory\n\nStable fact.\n"
    assert version["digest"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert version["byte_size"] == len(canonical.encode())
    assert version["review_status"] == "PENDING"
    assert version["sensitive_data_status"] == "NOT_SCANNED"
    assert version["lifecycle_state"] == "DRAFT"
    assert "content" not in response.text

    read = client.get(f"/api/v1/memory-sources/{source['id']}")
    assert read.status_code == 200
    assert "Stable fact" not in read.text
    with db_session_factory() as db:
        stored = db.get(MemorySourceVersion, version["id"])
        assert stored is not None and stored.content == canonical


def test_memory_source_revisions_are_append_only_and_governance_restarts(client):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    first = source["latest_version"]
    response = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions",
        json={"content": "# Project memory\n\nRevised fact."},
    )
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["version_no"] == 2
    assert second["previous_version_id"] == first["id"]
    assert second["review_status"] == "PENDING"
    assert second["sensitive_data_status"] == "NOT_SCANNED"
    assert second["lifecycle_state"] == "DRAFT"

    unchanged = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions",
        json={"content": "# Project memory\r\n\r\nRevised fact.\r\n"},
    )
    assert unchanged.status_code == 409
    assert unchanged.json()["error"]["code"] == "MEMORY_SOURCE_VERSION_UNCHANGED"


def test_memory_source_rejects_claimed_governance_and_invalid_content(client):
    claimed = _payload()
    claimed["review_status"] = "APPROVED"
    response = client.post("/api/v1/memory-sources", json=claimed)
    assert response.status_code == 422

    invalid = client.post("/api/v1/memory-sources", json=_payload("unsafe\x00content"))
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "MEMORY_SOURCE_CONTENT_INVALID"


def test_database_blocks_memory_content_identity_mutation_and_deletion(client, db_session_factory):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    version_id = source["latest_version"]["id"]
    with db_session_factory() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySourceVersion)
                .where(MemorySourceVersion.id == version_id)
                .values(content="tampered\n")
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(delete(MemorySourceVersion).where(MemorySourceVersion.id == version_id))
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySource)
                .where(MemorySource.id == source["id"])
                .values(owner_id="other-user")
            )
            db.commit()


def test_memory_source_review_scan_and_activation_require_both_gates(client):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    version = source["latest_version"]

    early = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={"expected_governance_version": 1},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "MEMORY_SOURCE_ACTIVATION_BLOCKED"

    missing_actor = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={
            "expected_governance_version": 1,
            "decision": "APPROVE",
        },
    )
    assert missing_actor.status_code == 422

    blank_actor = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={"expected_governance_version": 1, "decision": "APPROVE"},
        headers={"X-Actor-ID": "   "},
    )
    assert blank_actor.status_code == 422
    assert blank_actor.json()["error"]["code"] == "MEMORY_SOURCE_REVIEW_ACTOR_REQUIRED"

    self_review = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={
            "expected_governance_version": 1,
            "decision": "APPROVE",
        },
        headers={"X-Actor-ID": "user-123"},
    )
    assert self_review.status_code == 403
    reviewed = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={
            "expected_governance_version": 1,
            "decision": "APPROVE",
            "note": "Reviewed against project policy",
        },
        headers={"X-Actor-ID": "reviewer-456"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["governance_version"] == 2
    still_early = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={"expected_governance_version": 2},
    )
    assert still_early.status_code == 409

    scanned = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/scan",
        json={"expected_governance_version": 2},
    )
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["sensitive_data_status"] == "PASSED"
    assert scanned.json()["sensitive_data_report"]["finding_count"] == 0
    assert scanned.json()["governance_version"] == 3

    stale = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={"expected_governance_version": 2},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "MEMORY_SOURCE_GOVERNANCE_VERSION_CONFLICT"
    activated = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={"expected_governance_version": 3},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["lifecycle_state"] == "ACTIVE"
    assert activated.json()["governance_version"] == 4


def test_memory_source_scan_blocks_secrets_without_echoing_values(client):
    secret = "super-secret-value-123"
    source = client.post(
        "/api/v1/memory-sources", json=_payload(f"# Memory\n\napi_key={secret}\n")
    ).json()
    version = source["latest_version"]
    response = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/scan",
        json={"expected_governance_version": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sensitive_data_status"] == "BLOCKED"
    assert response.json()["sensitive_data_report"]["findings"] == [
        {"category": "SECRET_ASSIGNMENT", "count": 1}
    ]
    assert secret not in response.text

    review = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
        json={
            "expected_governance_version": 2,
            "decision": "APPROVE",
        },
        headers={"X-Actor-ID": "reviewer-456"},
    )
    assert review.status_code == 200
    activation = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
        json={"expected_governance_version": 3},
    )
    assert activation.status_code == 409
    assert activation.json()["error"]["code"] == "MEMORY_SOURCE_ACTIVATION_BLOCKED"


def test_activating_new_memory_version_atomically_retires_previous(client, db_session_factory):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    first = source["latest_version"]

    def govern(version: dict[str, object], expected: int) -> dict[str, object]:
        reviewed = client.post(
            f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/review",
            json={
                "expected_governance_version": expected,
                "decision": "APPROVE",
            },
            headers={"X-Actor-ID": "reviewer-456"},
        ).json()
        scanned = client.post(
            f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/scan",
            json={"expected_governance_version": reviewed["governance_version"]},
        ).json()
        return client.post(
            f"/api/v1/memory-sources/{source['id']}/versions/{version['id']}/activate",
            json={"expected_governance_version": scanned["governance_version"]},
        ).json()

    govern(first, 1)
    second = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions",
        json={"content": "# Project memory\n\nReplacement fact."},
    ).json()
    govern(second, 1)

    with db_session_factory() as db:
        versions = list(
            db.scalars(
                select(MemorySourceVersion)
                .where(MemorySourceVersion.source_id == source["id"])
                .order_by(MemorySourceVersion.version_no)
            )
        )
        assert [item.lifecycle_state for item in versions] == ["RETIRED", "ACTIVE"]
        assert versions[0].retired_at is not None
        assert versions[1].activated_at is not None


def test_database_blocks_bypassing_memory_governance_transitions(client, db_session_factory):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    version_id = source["latest_version"]["id"]
    with db_session_factory() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySourceVersion)
                .where(MemorySourceVersion.id == version_id)
                .values(lifecycle_state="ACTIVE", governance_version=2)
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySourceVersion)
                .where(MemorySourceVersion.id == version_id)
                .values(
                    review_status="APPROVED",
                    sensitive_data_status="PASSED",
                    governance_version=2,
                )
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(delete(MemorySourceVersion).where(MemorySourceVersion.id == version_id))
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySource)
                .where(MemorySource.id == source["id"])
                .values(owner_id="other-user")
            )
            db.commit()


def test_memory_source_retention_is_frozen_and_blocks_early_expiry(client):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    active = _govern_and_activate(client, source, retention_days=45)
    assert active["retention_days"] == 45
    assert active["expires_at"] is None

    active_delete = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/delete",
        json={"expected_governance_version": active["governance_version"]},
    )
    assert active_delete.status_code == 409
    assert active_delete.json()["error"]["code"] == "MEMORY_SOURCE_DELETION_BLOCKED"

    retired = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/retire",
        json={"expected_governance_version": active["governance_version"]},
    )
    assert retired.status_code == 200, retired.text
    retired_body = retired.json()
    assert retired_body["lifecycle_state"] == "RETIRED"
    assert retired_body["retention_days"] == 45
    assert datetime.fromisoformat(retired_body["expires_at"]) == (
        datetime.fromisoformat(retired_body["retired_at"]) + timedelta(days=45)
    )

    early = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/expire",
        json={"expected_governance_version": retired_body["governance_version"]},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "MEMORY_SOURCE_NOT_EXPIRED"


def test_expired_memory_content_is_irreversibly_deleted_to_a_tombstone(
    client, db_session_factory, monkeypatch
):
    historical_activated_at = datetime.now(UTC) - timedelta(days=3)
    monkeypatch.setattr(memory_sources, "now", lambda: historical_activated_at)
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    active = _govern_and_activate(client, source, retention_days=1)
    historical_retired_at = datetime.now(UTC) - timedelta(days=2)
    monkeypatch.setattr(memory_sources, "now", lambda: historical_retired_at)
    retired = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/retire",
        json={"expected_governance_version": active["governance_version"]},
    )
    assert retired.status_code == 200, retired.text
    monkeypatch.setattr(memory_sources, "now", lambda: datetime.now(UTC))

    expired = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/expire",
        json={"expected_governance_version": retired.json()["governance_version"]},
    )
    assert expired.status_code == 200, expired.text
    deleted = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/delete",
        json={"expected_governance_version": expired.json()["governance_version"]},
    )
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body == {"id": active["id"], "source_id": source["id"], "deleted": True}
    assert "Stable fact" not in deleted.text

    with db_session_factory() as db:
        assert db.get(MemorySourceVersion, active["id"]) is None
        assert db.get(MemorySource, source["id"]) is None


def test_memory_source_reference_blocks_deletion_and_is_database_immutable(
    client, db_session_factory, monkeypatch
):
    historical_activated_at = datetime.now(UTC) - timedelta(days=3)
    monkeypatch.setattr(memory_sources, "now", lambda: historical_activated_at)
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    active = _govern_and_activate(client, source, retention_days=1)
    with db_session_factory() as db:
        db.add(
            MemorySourceVersionReference(
                memory_source_version_id=active["id"],
                reference_kind="POLICY_VERSION",
                reference_id=str(uuid4()),
            )
        )
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
        reference_id = _create_memory_policy_reference(db, active)

    historical_retired_at = datetime.now(UTC) - timedelta(days=2)
    monkeypatch.setattr(memory_sources, "now", lambda: historical_retired_at)
    retired = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/retire",
        json={"expected_governance_version": active["governance_version"]},
    )
    assert retired.status_code == 200, retired.text
    monkeypatch.setattr(memory_sources, "now", lambda: datetime.now(UTC))
    expired = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/expire",
        json={"expected_governance_version": retired.json()["governance_version"]},
    )
    assert expired.status_code == 200, expired.text

    blocked = client.post(
        f"/api/v1/memory-sources/{source['id']}/versions/{active['id']}/delete",
        json={"expected_governance_version": expired.json()["governance_version"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MEMORY_SOURCE_REFERENCED"

    with db_session_factory() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                delete(MemorySourceVersionReference).where(
                    MemorySourceVersionReference.id == reference_id
                )
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                update(MemorySourceVersion)
                .where(MemorySourceVersion.id == active["id"])
                .values(
                    lifecycle_state="DELETED",
                    content="",
                    governance_version=expired.json()["governance_version"] + 1,
                )
            )
            db.commit()


def test_database_rejects_retention_reference_to_non_active_version(client, db_session_factory):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    draft_id = source["latest_version"]["id"]
    with db_session_factory() as db:
        db.add(
            MemorySourceVersionReference(
                memory_source_version_id=draft_id,
                reference_kind="RUN_SNAPSHOT",
                reference_id=str(uuid4()),
            )
        )
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()


def test_snapshot_reference_requires_matching_frozen_memory_source(client, db_session_factory):
    source = client.post("/api/v1/memory-sources", json=_payload()).json()
    active = _govern_and_activate(client, source)
    identity = uuid4().hex
    with db_session_factory() as db:
        flow = FlowDefinition(
            name=f"memory-reference-{identity}",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="Memory reference run",
        )
        db.add(run)
        db.flush()

        def manifest(digest: str) -> dict[str, object]:
            return {
                "schema_version": 2,
                "nodes": {
                    "node-1": {
                        "agent_spec": {
                            "memory_policy": {
                                "runtime_config": {
                                    "source_refs": [
                                        {"reference_id": active["id"], "digest": digest}
                                    ]
                                }
                            }
                        }
                    }
                },
            }

        bad_snapshot = RunSnapshot(
            flow_run_id=run.id,
            version=1,
            schema_version=2,
            definition_json={},
            definition_hash="a" * 64,
            runtime_manifest_json=manifest("0" * 64),
            runtime_manifest_hash="b" * 64,
        )
        good_snapshot = RunSnapshot(
            flow_run_id=run.id,
            version=2,
            schema_version=2,
            definition_json={},
            definition_hash="c" * 64,
            runtime_manifest_json=manifest(str(active["digest"])),
            runtime_manifest_hash="d" * 64,
        )
        db.add_all([bad_snapshot, good_snapshot])
        db.commit()
        bad_snapshot_id = bad_snapshot.id
        good_snapshot_id = good_snapshot.id

    with db_session_factory() as db:
        db.add(
            MemorySourceVersionReference(
                memory_source_version_id=str(active["id"]),
                reference_kind="RUN_SNAPSHOT",
                reference_id=bad_snapshot_id,
            )
        )
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()

        db.add(
            MemorySourceVersionReference(
                memory_source_version_id=str(active["id"]),
                reference_kind="RUN_SNAPSHOT",
                reference_id=good_snapshot_id,
            )
        )
        db.commit()
