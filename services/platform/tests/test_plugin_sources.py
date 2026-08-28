from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from flowweave.modules.catalog.application import plugin_sources
from flowweave.shared.application.plugin_resolver import (
    MarketplacePluginResolveRequest,
    PluginResolveBundle,
    PluginResolveRequest,
)
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    BackgroundTask,
    CapabilityValidation,
    PluginSourceResolution,
)
from flowweave.shared.plugin_resolver import plugin_resolver_context
from flowweave.shared.settings import settings_context

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
SOURCE = "https://github.com/acme/governed-plugin.git"
MARKETPLACE_SOURCE = "https://github.com/acme/extensions.git"


def _plugin_bundle() -> tuple[bytes, dict[str, str]]:
    files = {
        ".plugin/plugin.json": json.dumps(
            {"name": "governed-review", "version": "1.0.0"},
            sort_keys=True,
        ).encode(),
        "commands/review.md": b"---\nname: review\n---\nReview the change.\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue(), {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }


class _Resolver:
    def __init__(self, db, content: bytes, file_hashes: dict[str, str]) -> None:
        self.db = db
        self.content = content
        self.file_hashes = file_hashes
        self.requests: list[PluginResolveRequest] = []
        self.marketplace_requests: list[MarketplacePluginResolveRequest] = []
        self.transaction_states: list[bool] = []

    def resolve(self, request: PluginResolveRequest) -> PluginResolveBundle:
        self.requests.append(request)
        self.transaction_states.append(self.db.in_transaction())
        return PluginResolveBundle(
            self.content,
            request.commit,
            {
                "schema_version": 1,
                "openhands_version": "1.40.0",
                "file_hashes": self.file_hashes,
            },
        )

    def resolve_marketplace_plugin(
        self, request: MarketplacePluginResolveRequest
    ) -> PluginResolveBundle:
        self.marketplace_requests.append(request)
        self.transaction_states.append(self.db.in_transaction())
        return PluginResolveBundle(
            self.content,
            COMMIT_B,
            {
                "schema_version": 1,
                "openhands_version": "1.44.0",
                "file_hashes": self.file_hashes,
                "source_kind": "MARKETPLACE",
            },
            resolved_source=SOURCE,
            resolved_repo_path="plugins/review",
        )


def _create(client, commit: str = COMMIT_A) -> dict[str, Any]:
    response = client.post(
        "/api/v1/plugin-source-resolutions",
        json={"source_url": SOURCE, "commit": commit, "repo_path": None},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _create_marketplace(client) -> dict[str, Any]:
    response = client.post(
        "/api/v1/plugin-source-resolutions/marketplace",
        json={
            "marketplace_source_url": MARKETPLACE_SOURCE,
            "marketplace_commit": COMMIT_A,
            "marketplace_repo_path": None,
            "plugin_name": "governed-review",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _resolve(
    db_session_factory, worker_container, resolution_id: str, content: bytes, hashes: dict[str, str]
) -> _Resolver:
    with db_session_factory() as db:
        resolver = _Resolver(db, content, hashes)
        with (
            settings_context(worker_container.settings),
            artifact_store_context(worker_container.artifact_store),
            plugin_resolver_context(resolver),
        ):
            plugin_sources.process_resolution(db, resolution_id)
        return resolver


def test_remote_plugin_resolution_publishes_only_frozen_local_bytes(
    worker_client, db_session_factory, worker_container
):
    content, hashes = _plugin_bundle()
    created = _create(worker_client)
    assert created["state"] == "PENDING"
    assert created["state_version"] == 1

    resolver = _resolve(db_session_factory, worker_container, created["id"], content, hashes)
    assert resolver.transaction_states == [False]
    assert resolver.requests == [PluginResolveRequest(SOURCE, COMMIT_A, None)]

    ready = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert ready["state"] == "READY"
    assert ready["state_version"] == 2
    assert ready["content_hash"] == hashlib.sha256(content).hexdigest()

    published_response = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{created['id']}/publish",
        json={"expected_state_version": 2},
    )
    assert published_response.status_code == 201, published_response.text
    published = published_response.json()
    assert published["state"] == "PUBLISHED"
    assert published["state_version"] == 3
    capability = published["capability"]
    config = capability["normalized_config"]
    assert config["content_hash"] == hashlib.sha256(content).hexdigest()
    assert config["storage_key"].startswith("plugin-sources/")
    assert "source_url" not in config
    assert "ref" not in config

    replay = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{created['id']}/publish",
        json={"expected_state_version": 2},
    )
    assert replay.status_code == 201
    assert replay.json()["capability"]["capability_id"] == capability["capability_id"]
    stale = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{created['id']}/publish",
        json={"expected_state_version": 1},
    )
    assert stale.status_code == 409

    with db_session_factory() as db:
        validations = db.scalars(
            select(CapabilityValidation).where(
                CapabilityValidation.capability_version_id == capability["capability_id"]
            )
        ).all()
        git_reports = [
            item.report_json for item in validations if item.validator == "flowweave-git-plugin-v1"
        ]
        assert len(git_reports) == 1
        assert git_reports[0]["source_url"] == SOURCE
        assert git_reports[0]["resolved_commit"] == COMMIT_A


def test_marketplace_plugin_freezes_catalog_and_plugin_commits_before_publication(
    worker_client, db_session_factory, worker_container
):
    content, hashes = _plugin_bundle()
    created = _create_marketplace(worker_client)
    assert created["source_kind"] == "MARKETPLACE"
    assert created["source_url"] == MARKETPLACE_SOURCE
    assert created["requested_commit"] == COMMIT_A
    assert created["marketplace_plugin_name"] == "governed-review"

    resolver = _resolve(db_session_factory, worker_container, created["id"], content, hashes)
    assert resolver.transaction_states == [False]
    assert resolver.marketplace_requests == [
        MarketplacePluginResolveRequest(MARKETPLACE_SOURCE, COMMIT_A, None, "governed-review")
    ]

    ready = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert ready["state"] == "READY"
    assert ready["resolved_source_url"] == SOURCE
    assert ready["resolved_commit"] == COMMIT_B
    assert ready["resolved_repo_path"] == "plugins/review"

    response = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{created['id']}/publish",
        json={"expected_state_version": ready["state_version"]},
    )
    assert response.status_code == 201, response.text
    published = response.json()
    config = published["capability"]["normalized_config"]
    assert config["content_hash"] == hashlib.sha256(content).hexdigest()
    assert "source_url" not in config
    assert "marketplace" not in config

    with db_session_factory() as db:
        report = db.scalar(
            select(CapabilityValidation.report_json).where(
                CapabilityValidation.capability_version_id
                == published["capability"]["capability_id"],
                CapabilityValidation.validator == "flowweave-marketplace-plugin-v1",
            )
        )
    assert report is not None
    assert report["source_url"] == MARKETPLACE_SOURCE
    assert report["requested_commit"] == COMMIT_A
    assert report["resolved_source_url"] == SOURCE
    assert report["resolved_commit"] == COMMIT_B


@pytest.mark.parametrize(
    ("resolved_source", "error"),
    (
        (None, "omitted the immutable Plugin source"),
        ("https://example.com/acme/plugin.git", "allowed host"),
    ),
)
def test_marketplace_plugin_rejects_untrusted_resolved_coordinates(
    worker_client,
    db_session_factory,
    worker_container,
    resolved_source: str | None,
    error: str,
):
    content, hashes = _plugin_bundle()
    created = _create_marketplace(worker_client)

    class InvalidMarketplaceResolver(_Resolver):
        def resolve_marketplace_plugin(
            self, request: MarketplacePluginResolveRequest
        ) -> PluginResolveBundle:
            bundle = super().resolve_marketplace_plugin(request)
            return PluginResolveBundle(
                bundle.content,
                bundle.resolved_commit,
                bundle.report,
                resolved_source=resolved_source,
                resolved_repo_path=bundle.resolved_repo_path,
            )

    with db_session_factory() as db:
        resolver = InvalidMarketplaceResolver(db, content, hashes)
        with (
            settings_context(worker_container.settings),
            artifact_store_context(worker_container.artifact_store),
            plugin_resolver_context(resolver),
        ):
            with pytest.raises((RuntimeError, ValueError), match=error):
                plugin_sources.process_resolution(db, created["id"])

    current = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert current["state"] == "PENDING"
    assert current["content_hash"] is None


def test_remote_plugin_publish_rejects_object_drift_and_retains_ready_state(
    worker_client, db_session_factory, worker_container
):
    content, hashes = _plugin_bundle()
    created = _create(worker_client)
    _resolve(db_session_factory, worker_container, created["id"], content, hashes)
    with db_session_factory() as db:
        item = db.get(PluginSourceResolution, created["id"])
        assert item is not None and item.storage_key
        storage_key = item.storage_key
    worker_container.artifact_store.put(storage_key, b"tampered")

    response = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{created['id']}/publish",
        json={"expected_state_version": 2},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLUGIN_SOURCE_INVALID"
    current = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert current["state"] == "READY"
    assert current["state_version"] == 2


def test_expired_remote_plugin_never_invokes_network_resolver(
    worker_client, db_session_factory, worker_container
):
    created = _create(worker_client)
    with db_session_factory() as db:
        item = db.get(PluginSourceResolution, created["id"])
        assert item is not None
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        class RejectResolver:
            def resolve(self, _request: PluginResolveRequest) -> PluginResolveBundle:
                raise AssertionError("expired resolution must not access Git")

        with (
            settings_context(worker_container.settings),
            artifact_store_context(worker_container.artifact_store),
            plugin_resolver_context(RejectResolver()),
        ):
            plugin_sources.process_resolution(db, created["id"])

    current = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert current["state"] == "EXPIRED"
    assert current["state_version"] == 2


def test_failed_remote_plugin_retries_with_new_task_generation(worker_client, db_session_factory):
    created = _create(worker_client)
    with db_session_factory() as db:
        plugin_sources.record_resolution_failure(db, created["id"], "fetch failed")
    failed = worker_client.get(f"/api/v1/plugin-source-resolutions/{created['id']}").json()
    assert failed["state"] == "FAILED"
    assert failed["state_version"] == 2

    retried = _create(worker_client)
    assert retried["id"] == created["id"]
    assert retried["state"] == "PENDING"
    assert retried["state_version"] == 3
    with db_session_factory() as db:
        keys = set(
            db.scalars(
                select(BackgroundTask.idempotency_key).where(
                    BackgroundTask.aggregate_id == created["id"]
                )
            ).all()
        )
    assert f"resolve-plugin-source:{created['id']}:1" in keys
    assert f"resolve-plugin-source:{created['id']}:3" in keys
    assert f"expire-plugin-source:{created['id']}:3" in keys


def test_distinct_commits_with_identical_plugin_reuse_version_and_keep_source_audit(
    worker_client, db_session_factory, worker_container
):
    content, hashes = _plugin_bundle()
    first = _create(worker_client, COMMIT_A)
    _resolve(db_session_factory, worker_container, first["id"], content, hashes)
    first_published = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{first['id']}/publish",
        json={"expected_state_version": 2},
    ).json()

    second = _create(worker_client, COMMIT_B)
    _resolve(db_session_factory, worker_container, second["id"], content, hashes)
    second_response = worker_client.post(
        f"/api/v1/plugin-source-resolutions/{second['id']}/publish",
        json={"expected_state_version": 2},
    )
    assert second_response.status_code == 201, second_response.text
    second_published = second_response.json()
    version_id = first_published["capability"]["capability_id"]
    assert second_published["capability"]["capability_id"] == version_id

    with db_session_factory() as db:
        reports = [
            item.report_json
            for item in db.scalars(
                select(CapabilityValidation).where(
                    CapabilityValidation.capability_version_id == version_id,
                    CapabilityValidation.validator == "flowweave-git-plugin-v1",
                )
            ).all()
        ]
    assert {report["resolved_commit"] for report in reports} == {COMMIT_A, COMMIT_B}


def test_publish_confirmation_fences_state_drift(
    worker_client, db_session_factory, worker_container
):
    content, hashes = _plugin_bundle()
    created = _create(worker_client)
    _resolve(db_session_factory, worker_container, created["id"], content, hashes)
    with (
        settings_context(worker_container.settings),
        artifact_store_context(worker_container.artifact_store),
        db_session_factory() as db,
    ):
        plan = plugin_sources.prepare_publish_resolution(db, created["id"], 2)
        assert not isinstance(plan, dict)
        db.rollback()
        plugin_sources.verify_publish_source(plan)
        item = db.get(PluginSourceResolution, created["id"])
        assert item is not None
        item.state_version += 1
        db.commit()
        with pytest.raises(DomainError) as raised:
            plugin_sources.confirm_publish_resolution(db, plan)
    assert raised.value.code == "PLUGIN_SOURCE_STATE_CONFLICT"
