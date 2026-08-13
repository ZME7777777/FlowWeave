"""install the default Tool Policy and freeze RuntimeAgentSpec

Revision ID: 0031_tool_policy_runtime_spec
Revises: 0030_snapshot_runtime_manifest
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0031_tool_policy_runtime_spec"
down_revision = "0030_snapshot_runtime_manifest"
branch_labels = None
depends_on = None

OPENHANDS_VERSION = "1.40.0"
POLICY_KEY = "flowweave-default-tools"
POLICY_CONFIG: dict[str, object] = {
    "description": "FlowWeave default OpenHands 1.40.0 tool policy",
    "tools": [
        {"name": "terminal", "params": {}},
        {"name": "file_editor", "params": {}},
        {"name": "task_tracker", "params": {}},
    ],
}


def _id(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"flowweave:{kind}:{value}"))


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


POLICY_BYTES = json.dumps(
    POLICY_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode()
POLICY_CONTENT_HASH = hashlib.sha256(POLICY_BYTES).hexdigest()
POLICY_BLOB_ID = _id("blob", POLICY_CONTENT_HASH)
POLICY_PACKAGE_ID = _id("package", f"TOOL_POLICY:{POLICY_KEY}")
POLICY_VERSION_ID = _id("version", f"builtin:{POLICY_KEY}:1")
POLICY_VALIDATION_ID = _id("validation", POLICY_VERSION_ID)
POLICY_DIGEST = _hash(
    {
        "capability_type": "TOOL_POLICY",
        "capability_key": POLICY_KEY,
        "content_hash": POLICY_CONTENT_HASH,
        "normalized_config": POLICY_CONFIG,
    }
)


def _runtime_config() -> dict[str, object]:
    return {
        **POLICY_CONFIG,
        "capability_id": POLICY_VERSION_ID,
        "capability_version_id": POLICY_VERSION_ID,
        "package_id": POLICY_PACKAGE_ID,
        "version_no": 1,
        "digest": POLICY_DIGEST,
        "filename": "flowweave-default-tools.json",
        "content_hash": POLICY_CONTENT_HASH,
        "storage_key": f"builtin://tool-policies/{POLICY_CONTENT_HASH}.json",
    }


def _frozen_policy() -> dict[str, object]:
    return {
        "capability_version_id": POLICY_VERSION_ID,
        "capability_type": "TOOL_POLICY",
        "capability_key": POLICY_KEY,
        "digest": POLICY_DIGEST,
        "content_hash": POLICY_CONTENT_HASH,
        "runtime_config": _runtime_config(),
    }


def _seed_policy(bind: sa.Connection) -> None:
    now = datetime.now(UTC)
    bind.execute(
        sa.text(
            "INSERT INTO capability_blobs "
            "(id, content_hash, storage_key, byte_size, media_type, created_at) "
            "VALUES (:id, :content_hash, :storage_key, :byte_size, "
            "'application/json', :created_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_BLOB_ID,
            "content_hash": POLICY_CONTENT_HASH,
            "storage_key": f"builtin://tool-policies/{POLICY_CONTENT_HASH}.json",
            "byte_size": len(POLICY_BYTES),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_packages "
            "(id, capability_type, capability_key, display_name, description, "
            "created_at, updated_at) VALUES (:id, 'TOOL_POLICY', :key, :name, "
            ":description, :created_at, :updated_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_PACKAGE_ID,
            "key": POLICY_KEY,
            "name": "FlowWeave Default Tools",
            "description": POLICY_CONFIG["description"],
            "created_at": now,
            "updated_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_versions "
            "(id, package_id, blob_id, version_no, digest, normalized_config_json, "
            "source_filename, source_import_id, source_position, state, created_at) "
            "VALUES (:id, :package_id, :blob_id, 1, :digest, CAST(:config AS JSON), "
            "'flowweave-default-tools.json', NULL, NULL, 'PUBLISHED', :created_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_VERSION_ID,
            "package_id": POLICY_PACKAGE_ID,
            "blob_id": POLICY_BLOB_ID,
            "digest": POLICY_DIGEST,
            "config": json.dumps(POLICY_CONFIG, ensure_ascii=False),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_validations "
            "(id, capability_version_id, validator, status, report_json, created_at) "
            "VALUES (:id, :version_id, 'flowweave-builtin-v1', 'PASSED', "
            "CAST(:report AS JSON), :created_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_VALIDATION_ID,
            "version_id": POLICY_VERSION_ID,
            "report": json.dumps({"builtin": True, "openhands_version": OPENHANDS_VERSION}),
            "created_at": now,
        },
    )


def _backfill_node_bindings(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT a.id, COALESCE(MAX(r.position), -1) AS max_position "
            "FROM node_assets a LEFT JOIN node_capability_refs r "
            "ON r.node_asset_id = a.id "
            "WHERE NOT EXISTS (SELECT 1 FROM node_capability_refs p "
            "WHERE p.node_asset_id = a.id AND p.capability_type = 'TOOL_POLICY') "
            "GROUP BY a.id ORDER BY a.id"
        )
    ).mappings()
    config = json.dumps(_runtime_config(), ensure_ascii=False)
    for row in rows:
        ref_id = _id("node-tool-policy", f"{row['id']}:{POLICY_VERSION_ID}")
        bind.execute(
            sa.text(
                "INSERT INTO node_capability_refs "
                "(id, node_asset_id, capability_type, capability_key, "
                "capability_version_id, normalized_config, position) "
                "VALUES (:id, :node_id, 'TOOL_POLICY', :key, :version_id, "
                "CAST(:config AS JSON), :position)"
            ),
            {
                "id": ref_id,
                "node_id": row["id"],
                "key": POLICY_KEY,
                "version_id": POLICY_VERSION_ID,
                "config": config,
                "position": int(row["max_position"]) + 1,
            },
        )


def _definition_nodes(definition: object) -> dict[str, dict[str, object]]:
    if not isinstance(definition, dict) or not isinstance(definition.get("nodes"), list):
        raise RuntimeError("Cannot compile Runtime Agent Spec from Snapshot definition")
    result: dict[str, dict[str, object]] = {}
    for item in definition["nodes"]:
        if not isinstance(item, dict):
            raise RuntimeError("Snapshot contains an invalid node")
        key = str(item.get("instance_key") or "")
        asset = item.get("asset")
        if not key or not isinstance(asset, dict):
            raise RuntimeError("Snapshot contains an invalid node asset")
        result[key] = asset
    return result


def _upgrade_manifest(manifest: object, definition: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise RuntimeError("Snapshot Runtime Manifest is invalid")
    assets = _definition_nodes(definition)
    nodes: dict[str, object] = {}
    for key, raw_node in manifest["nodes"].items():
        if not isinstance(raw_node, dict) or not isinstance(raw_node.get("capabilities"), list):
            raise RuntimeError("Snapshot Runtime Manifest node is invalid")
        asset = assets.get(str(key))
        if asset is None:
            raise RuntimeError("Snapshot Runtime Manifest node has no definition")
        capabilities: list[object] = []
        policies: list[object] = []
        for capability in raw_node["capabilities"]:
            if not isinstance(capability, dict):
                raise RuntimeError("Snapshot Runtime Manifest capability is invalid")
            if capability.get("capability_type") == "TOOL_POLICY":
                policies.append(capability)
            else:
                capabilities.append(capability)
        # Be idempotent when a baseline migration has already emitted the v2
        # shape.  A custom frozen policy lives under agent_spec in v2 and must
        # never be replaced by the built-in default during repair/replay.
        existing_spec = raw_node.get("agent_spec")
        if isinstance(existing_spec, dict):
            existing_policy = existing_spec.get("tool_policy")
            if isinstance(existing_policy, dict):
                policies.append(existing_policy)
        if not policies:
            policies.append(_frozen_policy())
        if len(policies) != 1:
            raise RuntimeError("Snapshot must freeze exactly one Tool Policy")
        executor = asset.get("executor")
        if not isinstance(executor, dict):
            raise RuntimeError("Snapshot executor is invalid")
        nodes[str(key)] = {
            "node_asset_id": str(raw_node.get("node_asset_id") or ""),
            "capabilities": capabilities,
            "agent_spec": {
                "schema_version": 1,
                "agent_kind": "OPENHANDS",
                "openhands_version": OPENHANDS_VERSION,
                "tool_policy": policies[0],
                "confirmation_policy": str(executor.get("confirmation_policy") or "ALWAYS"),
                "condenser": executor.get("condenser") or {"kind": "NO_OP"},
                "budgets": {"max_iterations": int(executor.get("max_iterations") or 100)},
            },
        }
    return {
        "schema_version": 2,
        "openhands_version": OPENHANDS_VERSION,
        "nodes": nodes,
    }


def _downgrade_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise RuntimeError("Snapshot Runtime Manifest is invalid")
    nodes: dict[str, object] = {}
    for key, raw_node in manifest["nodes"].items():
        if not isinstance(raw_node, dict) or not isinstance(raw_node.get("capabilities"), list):
            raise RuntimeError("Snapshot Runtime Manifest node is invalid")
        capabilities = list(raw_node["capabilities"])
        agent_spec = raw_node.get("agent_spec")
        if isinstance(agent_spec, dict):
            policy = agent_spec.get("tool_policy")
            if (
                isinstance(policy, dict)
                and policy.get("capability_version_id") != POLICY_VERSION_ID
            ):
                capabilities.append(policy)
        nodes[str(key)] = {
            "node_asset_id": str(raw_node.get("node_asset_id") or ""),
            "capabilities": capabilities,
        }
    return {"schema_version": 1, "nodes": nodes}


def _rewrite_manifests(bind: sa.Connection, *, upgrading: bool) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, definition_json, runtime_manifest_json FROM run_snapshots "
            "ORDER BY created_at, id"
        )
    ).mappings()
    for row in rows:
        manifest = (
            _upgrade_manifest(row["runtime_manifest_json"], row["definition_json"])
            if upgrading
            else _downgrade_manifest(row["runtime_manifest_json"])
        )
        bind.execute(
            sa.text(
                "UPDATE run_snapshots SET runtime_manifest_json = CAST(:manifest AS JSON), "
                "runtime_manifest_hash = :digest WHERE id = :id"
            ),
            {
                "id": row["id"],
                "manifest": json.dumps(manifest, ensure_ascii=False),
                "digest": _hash(manifest),
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    _seed_policy(bind)
    _backfill_node_bindings(bind)
    _rewrite_manifests(bind, upgrading=True)


def downgrade() -> None:
    bind = op.get_bind()
    _rewrite_manifests(bind, upgrading=False)
    bind.execute(
        sa.text("DELETE FROM node_capability_refs WHERE capability_version_id = :version_id"),
        {"version_id": POLICY_VERSION_ID},
    )
    bind.execute(
        sa.text("DELETE FROM capability_validations WHERE id = :id"),
        {"id": POLICY_VALIDATION_ID},
    )
    bind.execute(
        sa.text("DELETE FROM capability_versions WHERE id = :id"),
        {"id": POLICY_VERSION_ID},
    )
    bind.execute(
        sa.text("DELETE FROM capability_packages WHERE id = :id"),
        {"id": POLICY_PACKAGE_ID},
    )
    bind.execute(
        sa.text(
            "DELETE FROM capability_blobs WHERE id = :id AND NOT EXISTS "
            "(SELECT 1 FROM capability_versions WHERE blob_id = :id)"
        ),
        {"id": POLICY_BLOB_ID},
    )
