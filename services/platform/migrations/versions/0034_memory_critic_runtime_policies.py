"""freeze fail-closed Memory and Critic policies in RuntimeAgentSpec

Revision ID: 0034_runtime_policies
Revises: 0033_plugin_source_resolutions
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0034_runtime_policies"
down_revision = "0033_plugin_source_resolutions"
branch_labels = None
depends_on = None

POLICIES: dict[str, tuple[str, dict[str, object], str]] = {
    "memory_policy": (
        "MEMORY_POLICY",
        {
            "description": "FlowWeave default fail-closed Memory policy",
            "enabled": False,
            "scopes": [],
            "source_refs": [],
            "retention_days": None,
            "require_review": True,
            "sensitive_data_scan": True,
            "replay_mode": "FROZEN",
        },
        "flowweave-memory-disabled",
    ),
    "critic_policy": (
        "CRITIC_POLICY",
        {
            "description": "FlowWeave default fail-closed Critic policy",
            "enabled": False,
            "mode": "FINISH_AND_MESSAGE",
            "threshold": 0.6,
            "max_refinement_iterations": 0,
        },
        "flowweave-critic-disabled",
    ),
}


def _id(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"flowweave:{kind}:{value}"))


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _frozen(field: str) -> dict[str, object]:
    capability_type, config, key = POLICIES[field]
    content_hash = _hash(config)
    version_id = _id("version", f"builtin:{key}:1")
    digest = _hash(
        {
            "capability_type": capability_type,
            "capability_key": key,
            "content_hash": content_hash,
            "normalized_config": config,
        }
    )
    runtime_config = {
        **config,
        "capability_id": version_id,
        "capability_version_id": version_id,
        "package_id": _id("package", f"{capability_type}:{key}"),
        "version_no": 1,
        "digest": digest,
        "filename": f"{key}.json",
        "content_hash": content_hash,
        "storage_key": f"builtin://runtime-policies/{content_hash}.json",
    }
    return {
        "capability_version_id": version_id,
        "capability_type": capability_type,
        "capability_key": key,
        "digest": digest,
        "content_hash": content_hash,
        "runtime_config": runtime_config,
    }


def _seed(bind: sa.Connection, field: str) -> None:
    capability_type, config, key = POLICIES[field]
    frozen = _frozen(field)
    now = datetime.now(UTC)
    content_hash = str(frozen["content_hash"])
    blob_id = _id("blob", content_hash)
    package_id = _id("package", f"{capability_type}:{key}")
    version_id = str(frozen["capability_version_id"])
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    bind.execute(
        sa.text(
            "INSERT INTO capability_blobs "
            "(id, content_hash, storage_key, byte_size, media_type, created_at) "
            "VALUES (:id, :hash, :key, :size, 'application/json', :created) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": blob_id,
            "hash": content_hash,
            "key": f"builtin://runtime-policies/{content_hash}.json",
            "size": len(encoded),
            "created": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_packages "
            "(id, capability_type, capability_key, display_name, description, "
            "created_at, updated_at) VALUES (:id, :type, :key, :name, :description, "
            ":created, :created) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": package_id,
            "type": capability_type,
            "key": key,
            "name": key,
            "description": config["description"],
            "created": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_versions "
            "(id, package_id, blob_id, version_no, digest, normalized_config_json, "
            "source_filename, source_import_id, source_position, state, created_at) "
            "VALUES (:id, :package, :blob, 1, :digest, CAST(:config AS JSON), "
            ":filename, NULL, NULL, 'PUBLISHED', :created) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": version_id,
            "package": package_id,
            "blob": blob_id,
            "digest": frozen["digest"],
            "config": json.dumps(config, ensure_ascii=False),
            "filename": f"{key}.json",
            "created": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_validations "
            "(id, capability_version_id, validator, status, report_json, created_at) "
            "VALUES (:id, :version, 'flowweave-builtin-v1', 'PASSED', "
            "CAST(:report AS JSON), :created) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": _id("validation", version_id),
            "version": version_id,
            "report": json.dumps(
                {"builtin": True, "openhands_version": "1.40.0", "enabled": False}
            ),
            "created": now,
        },
    )


def _backfill_bindings(bind: sa.Connection, field: str) -> None:
    capability_type, _, key = POLICIES[field]
    frozen = _frozen(field)
    rows = bind.execute(
        sa.text(
            "SELECT a.id, COALESCE(MAX(r.position), -1) AS max_position "
            "FROM node_assets a LEFT JOIN node_capability_refs r ON r.node_asset_id = a.id "
            "WHERE NOT EXISTS (SELECT 1 FROM node_capability_refs p "
            "WHERE p.node_asset_id = a.id AND p.capability_type = :type) "
            "GROUP BY a.id ORDER BY a.id"
        ),
        {"type": capability_type},
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO node_capability_refs "
                "(id, node_asset_id, capability_type, capability_key, "
                "capability_version_id, normalized_config, position) "
                "VALUES (:id, :node, :type, :key, :version, CAST(:config AS JSON), :position)"
            ),
            {
                "id": _id("node-runtime-policy", f"{row['id']}:{frozen['capability_version_id']}"),
                "node": row["id"],
                "type": capability_type,
                "key": key,
                "version": frozen["capability_version_id"],
                "config": json.dumps(frozen["runtime_config"], ensure_ascii=False),
                "position": int(row["max_position"]) + 1,
            },
        )


def _rewrite_manifests(bind: sa.Connection, *, upgrading: bool) -> None:
    rows = bind.execute(
        sa.text("SELECT id, runtime_manifest_json FROM run_snapshots ORDER BY created_at, id")
    ).mappings()
    for row in rows:
        manifest = row["runtime_manifest_json"]
        if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
            raise RuntimeError("Snapshot Runtime Manifest is invalid")
        nodes: dict[str, object] = {}
        for node_key, raw_node in manifest["nodes"].items():
            if not isinstance(raw_node, dict) or not isinstance(raw_node.get("agent_spec"), dict):
                raise RuntimeError("Snapshot Runtime Manifest node is invalid")
            agent_spec = dict(raw_node["agent_spec"])
            for field in POLICIES:
                if upgrading:
                    agent_spec.setdefault(field, _frozen(field))
                else:
                    frozen = agent_spec.get(field)
                    if (
                        isinstance(frozen, dict)
                        and frozen.get("capability_version_id")
                        == _frozen(field)["capability_version_id"]
                    ):
                        agent_spec.pop(field, None)
            nodes[str(node_key)] = {**raw_node, "agent_spec": agent_spec}
        rewritten = {**manifest, "nodes": nodes}
        bind.execute(
            sa.text(
                "UPDATE run_snapshots SET runtime_manifest_json = CAST(:manifest AS JSON), "
                "runtime_manifest_hash = :digest WHERE id = :id"
            ),
            {
                "id": row["id"],
                "manifest": json.dumps(rewritten, ensure_ascii=False),
                "digest": _hash(rewritten),
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    for field in POLICIES:
        _seed(bind, field)
        _backfill_bindings(bind, field)
    _rewrite_manifests(bind, upgrading=True)


def downgrade() -> None:
    bind = op.get_bind()
    _rewrite_manifests(bind, upgrading=False)
    for field in reversed(POLICIES):
        capability_type, _, key = POLICIES[field]
        frozen = _frozen(field)
        version_id = str(frozen["capability_version_id"])
        bind.execute(
            sa.text(
                "DELETE FROM node_capability_refs "
                "WHERE capability_type = :type AND capability_version_id = :version"
            ),
            {"type": capability_type, "version": version_id},
        )
        bind.execute(
            sa.text("DELETE FROM capability_validations WHERE capability_version_id = :id"),
            {"id": version_id},
        )
        bind.execute(
            sa.text("DELETE FROM capability_versions WHERE id = :id"),
            {"id": version_id},
        )
        package_id = _id("package", f"{capability_type}:{key}")
        bind.execute(
            sa.text("DELETE FROM capability_packages WHERE id = :id"),
            {"id": package_id},
        )
        blob_id = _id("blob", str(frozen["content_hash"]))
        bind.execute(
            sa.text(
                "DELETE FROM capability_blobs WHERE id = :id AND NOT EXISTS "
                "(SELECT 1 FROM capability_versions WHERE blob_id = :id)"
            ),
            {"id": blob_id},
        )
