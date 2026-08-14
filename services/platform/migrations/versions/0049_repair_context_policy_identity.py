"""repair malformed default Context Policy v1 wrappers

Revision ID: 0049_context_policy_identity
Revises: 0048_node_asset_name
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0049_context_policy_identity"
down_revision = "0048_node_asset_name"
branch_labels = None
depends_on = None

POLICY_KEY = "flowweave-default-context"
POLICY_VERSION_ID = str(uuid5(NAMESPACE_URL, f"flowweave:version:builtin:{POLICY_KEY}:1"))
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
BAD_POLICY_CONFIG = {
    **POLICY_CONFIG,
    "description": "FlowWeave default pinned OpenHands context policy",
}


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


POLICY_CONTENT_HASH = _json_hash(POLICY_CONFIG)
BAD_CONTENT_HASH = _json_hash(BAD_POLICY_CONFIG)
POLICY_DIGEST = _json_hash(
    {
        "capability_type": "CONTEXT_POLICY",
        "capability_key": POLICY_KEY,
        "content_hash": POLICY_CONTENT_HASH,
        "normalized_config": POLICY_CONFIG,
    }
)
POLICY_STORAGE_KEY = f"builtin://context-policies/{POLICY_CONTENT_HASH}.json"


def _repair_runtime_config(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    config = cast(dict[str, Any], value)
    if (
        config.get("capability_version_id") != POLICY_VERSION_ID
        or config.get("digest") != POLICY_DIGEST
        or config.get("content_hash") != BAD_CONTENT_HASH
    ):
        return False
    config["content_hash"] = POLICY_CONTENT_HASH
    config["storage_key"] = POLICY_STORAGE_KEY
    return True


def _repair_tree(value: object) -> bool:
    changed = False
    if isinstance(value, dict):
        item = cast(dict[str, Any], value)
        if (
            item.get("capability_version_id") == POLICY_VERSION_ID
            and item.get("capability_type") == "CONTEXT_POLICY"
            and item.get("capability_key") == POLICY_KEY
            and item.get("digest") == POLICY_DIGEST
            and item.get("content_hash") == BAD_CONTENT_HASH
        ):
            item["content_hash"] = POLICY_CONTENT_HASH
            changed = True
        changed = _repair_runtime_config(item.get("runtime_config")) or changed
        changed = _repair_runtime_config(item.get("normalized_config")) or changed
        for nested in item.values():
            changed = _repair_tree(nested) or changed
    elif isinstance(value, list):
        for nested in cast(list[object], value):
            changed = _repair_tree(nested) or changed
    return changed


def _repair_node_references(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, normalized_config FROM node_capability_refs "
            "WHERE capability_type = 'CONTEXT_POLICY' "
            "AND capability_key = :key AND capability_version_id = :version_id"
        ),
        {"key": POLICY_KEY, "version_id": POLICY_VERSION_ID},
    ).mappings()
    for row in rows:
        config = dict(row["normalized_config"] or {})
        if not _repair_runtime_config(config):
            continue
        bind.execute(
            sa.text(
                "UPDATE node_capability_refs SET normalized_config = CAST(:config AS JSON) "
                "WHERE id = :id"
            ),
            {"id": row["id"], "config": json.dumps(config, ensure_ascii=False)},
        )


def _repair_snapshots(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, definition_json, runtime_manifest_json FROM run_snapshots "
            "ORDER BY created_at, id"
        )
    ).mappings()
    for row in rows:
        definition = dict(row["definition_json"] or {})
        manifest = dict(row["runtime_manifest_json"] or {})
        definition_changed = _repair_tree(definition)
        manifest_changed = _repair_tree(manifest)
        if not definition_changed and not manifest_changed:
            continue
        values: dict[str, object] = {
            "id": row["id"],
            "definition": json.dumps(definition, ensure_ascii=False),
            "definition_hash": _json_hash(definition),
            "manifest": json.dumps(manifest, ensure_ascii=False),
            "manifest_hash": _json_hash(manifest),
        }
        bind.execute(
            sa.text(
                "UPDATE run_snapshots SET definition_json = CAST(:definition AS JSON), "
                "definition_hash = :definition_hash, "
                "runtime_manifest_json = CAST(:manifest AS JSON), "
                "runtime_manifest_hash = :manifest_hash WHERE id = :id"
            ),
            values,
        )


def upgrade() -> None:
    bind = op.get_bind()
    _repair_node_references(bind)
    _repair_snapshots(bind)


def downgrade() -> None:
    # This is a corruption repair. Reintroducing the mismatched Blob identity
    # on downgrade would make affected snapshots non-executable again.
    pass
