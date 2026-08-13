"""install the default Context Policy and freeze it in RuntimeAgentSpec

Revision ID: 0032_context_policy_runtime_spec
Revises: 0031_tool_policy_runtime_spec
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0032_context_policy_runtime_spec"
down_revision = "0031_tool_policy_runtime_spec"
branch_labels = None
depends_on = None

OPENHANDS_VERSION = "1.40.0"
POLICY_KEY = "flowweave-default-context"
POLICY_CONFIG: dict[str, object] = {
    "description": "FlowWeave default OpenHands 1.40.0 context policy",
    "system_message_suffix": "",
    "user_message_suffix": "",
    "load_user_skills": False,
    "load_public_skills": False,
    "marketplace_path": None,
    "load_project_skills": False,
    "registered_marketplaces": [],
    "disabled_skills": [],
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
POLICY_PACKAGE_ID = _id("package", f"CONTEXT_POLICY:{POLICY_KEY}")
POLICY_VERSION_ID = _id("version", f"builtin:{POLICY_KEY}:1")
POLICY_VALIDATION_ID = _id("validation", POLICY_VERSION_ID)
POLICY_DIGEST = _hash(
    {
        "capability_type": "CONTEXT_POLICY",
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
        "filename": f"{POLICY_KEY}.json",
        "content_hash": POLICY_CONTENT_HASH,
        "storage_key": f"builtin://context-policies/{POLICY_CONTENT_HASH}.json",
    }


def _frozen_policy() -> dict[str, object]:
    return {
        "capability_version_id": POLICY_VERSION_ID,
        "capability_type": "CONTEXT_POLICY",
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
            "storage_key": f"builtin://context-policies/{POLICY_CONTENT_HASH}.json",
            "byte_size": len(POLICY_BYTES),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO capability_packages "
            "(id, capability_type, capability_key, display_name, description, "
            "created_at, updated_at) VALUES (:id, 'CONTEXT_POLICY', :key, :name, "
            ":description, :created_at, :updated_at) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_PACKAGE_ID,
            "key": POLICY_KEY,
            "name": "FlowWeave Default Context",
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
            ":filename, NULL, NULL, 'PUBLISHED', :created_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": POLICY_VERSION_ID,
            "package_id": POLICY_PACKAGE_ID,
            "blob_id": POLICY_BLOB_ID,
            "digest": POLICY_DIGEST,
            "config": json.dumps(POLICY_CONFIG, ensure_ascii=False),
            "filename": f"{POLICY_KEY}.json",
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
            "report": json.dumps(
                {
                    "builtin": True,
                    "openhands_version": OPENHANDS_VERSION,
                    "memory_enabled": False,
                }
            ),
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
            "WHERE p.node_asset_id = a.id AND p.capability_type = 'CONTEXT_POLICY') "
            "GROUP BY a.id ORDER BY a.id"
        )
    ).mappings()
    config = json.dumps(_runtime_config(), ensure_ascii=False)
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO node_capability_refs "
                "(id, node_asset_id, capability_type, capability_key, "
                "capability_version_id, normalized_config, position) "
                "VALUES (:id, :node_id, 'CONTEXT_POLICY', :key, :version_id, "
                "CAST(:config AS JSON), :position)"
            ),
            {
                "id": _id("node-context-policy", f"{row['id']}:{POLICY_VERSION_ID}"),
                "node_id": row["id"],
                "key": POLICY_KEY,
                "version_id": POLICY_VERSION_ID,
                "config": config,
                "position": int(row["max_position"]) + 1,
            },
        )


def _upgrade_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise RuntimeError("Snapshot Runtime Manifest is invalid")
    nodes: dict[str, object] = {}
    for key, raw_node in manifest["nodes"].items():
        if (
            not isinstance(raw_node, dict)
            or not isinstance(raw_node.get("capabilities"), list)
            or not isinstance(raw_node.get("agent_spec"), dict)
        ):
            raise RuntimeError("Snapshot Runtime Manifest node is invalid")
        capabilities: list[object] = []
        policies: list[object] = []
        for capability in raw_node["capabilities"]:
            if not isinstance(capability, dict):
                raise RuntimeError("Snapshot Runtime Manifest capability is invalid")
            if capability.get("capability_type") == "CONTEXT_POLICY":
                policies.append(capability)
            else:
                capabilities.append(capability)
        agent_spec = dict(raw_node["agent_spec"])
        existing = agent_spec.get("context_policy")
        if isinstance(existing, dict):
            policies.append(existing)
        if not policies:
            policies.append(_frozen_policy())
        unique = {
            str(item.get("capability_version_id")) for item in policies if isinstance(item, dict)
        }
        if len(unique) != 1 or len(policies) != 1:
            raise RuntimeError("Snapshot must freeze exactly one Context Policy")
        agent_spec["context_policy"] = policies[0]
        nodes[str(key)] = {
            **raw_node,
            "capabilities": capabilities,
            "agent_spec": agent_spec,
        }
    return {**manifest, "nodes": nodes}


def _downgrade_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise RuntimeError("Snapshot Runtime Manifest is invalid")
    nodes: dict[str, object] = {}
    for key, raw_node in manifest["nodes"].items():
        if (
            not isinstance(raw_node, dict)
            or not isinstance(raw_node.get("capabilities"), list)
            or not isinstance(raw_node.get("agent_spec"), dict)
        ):
            raise RuntimeError("Snapshot Runtime Manifest node is invalid")
        capabilities = list(raw_node["capabilities"])
        agent_spec = dict(raw_node["agent_spec"])
        policy = agent_spec.pop("context_policy", None)
        if isinstance(policy, dict) and policy.get("capability_version_id") != POLICY_VERSION_ID:
            capabilities.append(policy)
        nodes[str(key)] = {
            **raw_node,
            "capabilities": capabilities,
            "agent_spec": agent_spec,
        }
    return {**manifest, "nodes": nodes}


def _rewrite_manifests(bind: sa.Connection, *, upgrading: bool) -> None:
    rows = bind.execute(
        sa.text("SELECT id, runtime_manifest_json FROM run_snapshots ORDER BY created_at, id")
    ).mappings()
    for row in rows:
        manifest = (
            _upgrade_manifest(row["runtime_manifest_json"])
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
