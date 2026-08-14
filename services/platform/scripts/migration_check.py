"""Run the PostgreSQL migration round trip in an isolated temporary database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb

LEGACY_IMPORT_ID = "10000000-0000-4000-8000-000000000001"
LEGACY_DUPLICATE_IMPORT_ID = "10000000-0000-4000-8000-000000000007"
LEGACY_ASSET_ID = "10000000-0000-4000-8000-000000000002"
LEGACY_DUPLICATE_ASSET_ID = "10000000-0000-4000-8000-000000000008"
LEGACY_REF_ID = "10000000-0000-4000-8000-000000000003"
LEGACY_DUPLICATE_REF_ID = "10000000-0000-4000-8000-000000000009"
LEGACY_FLOW_ID = "10000000-0000-4000-8000-000000000004"
LEGACY_RUN_ID = "10000000-0000-4000-8000-000000000005"
LEGACY_SNAPSHOT_ID = "10000000-0000-4000-8000-000000000006"
LEGACY_CONTENT_HASH = "a" * 64
DEFAULT_TOOL_POLICY_KEY = "flowweave-default-tools"
DEFAULT_CONTEXT_POLICY_KEY = "flowweave-default-context"


def assert_native_subagent_schema(connection_url: str) -> None:
    """The native projection must not retain the private delegation protocol."""

    with psycopg.connect(connection_url) as connection:
        task_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'runtime_subagent_tasks'"
            )
        }
        assert "action_event_id" in task_columns
        assert "observation_event_id" in task_columns
        assert "prompt" not in task_columns

        usage_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'runtime_subagent_task_usage'"
            )
        }
        assert {
            "runtime_subagent_task_id",
            "runtime_task_id",
            "snapshot_digest",
            "usage_version",
            "accumulated_cost_usd",
            "prompt_tokens",
            "completion_tokens",
            "budget_limit_usd",
            "budget_state",
        } <= usage_columns
        assert "prompt" not in usage_columns
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'runtime_subagent_task_usage'"
            )
        }
        assert {
            "uq_runtime_subagent_task_usage_task",
            "uq_runtime_subagent_task_usage_identity",
        } <= indexes

        conversation_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_conversations'"
            )
        }
        assert (
            not {
                "parent_conversation_id",
                "delegation_batch_key",
                "delegation_instruction",
            }
            & conversation_columns
        )


def insert_legacy_capability_snapshot(connection_url: str) -> None:
    """Insert a representative 0028-era Import, node reference, and Snapshot."""

    now = datetime.now(UTC)
    normalized = {
        "entry": "legacy-skill/SKILL.md",
        "description": "legacy Skill",
        "dependencies": {},
        "dependency_build_state": "NOT_REQUIRED",
        "capability_id": f"{LEGACY_IMPORT_ID}:0",
        "import_id": LEGACY_IMPORT_ID,
        "filename": "legacy-skill.zip",
        "content_hash": LEGACY_CONTENT_HASH,
        "storage_key": "capability-imports/legacy-skill.zip",
    }
    snapshot_normalized = {
        key: value for key, value in normalized.items() if key != "capability_id"
    }
    snapshot_normalized["import_id"] = LEGACY_DUPLICATE_IMPORT_ID
    capability = {
        "capability_id": f"{LEGACY_DUPLICATE_IMPORT_ID}:0",
        "capability_type": "SKILL",
        "capability_key": "legacy-skill",
        "normalized_config": snapshot_normalized,
        "position": 0,
    }
    definition = {
        "id": LEGACY_FLOW_ID,
        "name": "legacy flow",
        "row_version": 1,
        "nodes": [
            {
                "instance_key": "legacy-node",
                "node_asset_id": LEGACY_ASSET_ID,
                "asset": {
                    "id": LEGACY_ASSET_ID,
                    "name": "legacy node",
                    "inputs": [],
                    "outputs": [],
                    "executor": {"confirmation_policy": "ALWAYS"},
                    "capabilities": [capability],
                },
            }
        ],
        "edges": [],
        "port_mappings": [],
    }
    with psycopg.connect(connection_url) as connection:
        # Historical baselines create tables from current ORM metadata. Remove
        # fields owned by 0029/0030 so this fixture matches a real 0028 schema.
        connection.execute(
            "ALTER TABLE node_capability_refs DROP COLUMN IF EXISTS capability_version_id CASCADE"
        )
        connection.execute(
            "ALTER TABLE skill_collection_items DROP COLUMN IF EXISTS capability_version_id CASCADE"
        )
        connection.execute(
            "ALTER TABLE run_snapshots "
            "DROP COLUMN IF EXISTS runtime_manifest_json, "
            "DROP COLUMN IF EXISTS runtime_manifest_hash"
        )
        connection.execute(
            "INSERT INTO capability_imports "
            "(id, token_digest, capability_type, filename, content_hash, storage_key, "
            "byte_size, preview_json, state, expires_at, consumed_at, created_at) "
            "VALUES (%s, %s, 'SKILL', 'legacy-skill.zip', %s, %s, 42, %s, "
            "'COMMITTED', %s, %s, %s)",
            (
                LEGACY_IMPORT_ID,
                "b" * 64,
                LEGACY_CONTENT_HASH,
                "capability-imports/legacy-skill.zip",
                Jsonb(
                    {
                        "capabilities": [
                            {
                                "capability_key": "legacy-skill",
                                "normalized_config": {
                                    key: value
                                    for key, value in normalized.items()
                                    if key
                                    not in {
                                        "capability_id",
                                        "import_id",
                                        "filename",
                                        "content_hash",
                                        "storage_key",
                                    }
                                },
                            }
                        ]
                    }
                ),
                now + timedelta(days=1),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO capability_imports "
            "(id, token_digest, capability_type, filename, content_hash, storage_key, "
            "byte_size, preview_json, state, expires_at, consumed_at, created_at) "
            "SELECT %s, %s, capability_type, filename, content_hash, storage_key, "
            "byte_size, preview_json, state, expires_at, consumed_at + interval '1 second', "
            "created_at + interval '1 second' FROM capability_imports WHERE id = %s",
            (LEGACY_DUPLICATE_IMPORT_ID, "d" * 64, LEGACY_IMPORT_ID),
        )
        connection.execute(
            "INSERT INTO node_assets "
            "(id, directory_id, name, description, icon_kind, icon_value, "
            "environment_version_id, row_version, deleted_at, created_at, updated_at) "
            "VALUES (%s, NULL, 'legacy node', '', 'LUCIDE', 'bot', NULL, 1, NULL, %s, %s)",
            (LEGACY_ASSET_ID, now, now),
        )
        connection.execute(
            "INSERT INTO node_assets "
            "(id, directory_id, name, description, icon_kind, icon_value, "
            "environment_version_id, row_version, deleted_at, created_at, updated_at) "
            "VALUES (%s, NULL, 'duplicate legacy node', '', 'LUCIDE', 'bot', NULL, 1, "
            "NULL, %s, %s)",
            (LEGACY_DUPLICATE_ASSET_ID, now, now),
        )
        connection.execute(
            "INSERT INTO node_capability_refs "
            "(id, node_asset_id, capability_type, capability_key, normalized_config, position) "
            "VALUES (%s, %s, 'SKILL', 'legacy-skill', %s, 0)",
            (LEGACY_REF_ID, LEGACY_ASSET_ID, Jsonb(normalized)),
        )
        duplicate_normalized = {
            key: value for key, value in normalized.items() if key != "capability_id"
        }
        duplicate_normalized["import_id"] = LEGACY_DUPLICATE_IMPORT_ID
        connection.execute(
            "INSERT INTO node_capability_refs "
            "(id, node_asset_id, capability_type, capability_key, normalized_config, position) "
            "VALUES (%s, %s, 'SKILL', 'legacy-skill', %s, 0)",
            (
                LEGACY_DUPLICATE_REF_ID,
                LEGACY_DUPLICATE_ASSET_ID,
                Jsonb(duplicate_normalized),
            ),
        )
        connection.execute(
            "INSERT INTO flow_definitions "
            "(id, name, description, default_entry_key, lark_root_folder_url, row_version, "
            "deleted_at, created_at, updated_at) "
            "VALUES (%s, 'legacy flow', '', 'legacy-node', '', 1, NULL, %s, %s)",
            (LEGACY_FLOW_ID, now, now),
        )
        connection.execute(
            "INSERT INTO flow_runs "
            "(id, flow_definition_id, run_no, name, state, active_snapshot_id, "
            "environment_version_id, row_version, completion_mode, lark_folder_token, "
            "lark_folder_url, started_at, finished_at) "
            "VALUES (%s, %s, 1, 'legacy run', 'ACTIVE', NULL, NULL, 1, NULL, NULL, NULL, %s, NULL)",
            (LEGACY_RUN_ID, LEGACY_FLOW_ID, now),
        )
        connection.execute(
            "INSERT INTO run_snapshots "
            "(id, flow_run_id, version, schema_version, definition_json, definition_hash, "
            "created_by_action_id, created_at) "
            "VALUES (%s, %s, 1, 1, %s, %s, NULL, %s)",
            (LEGACY_SNAPSHOT_ID, LEGACY_RUN_ID, Jsonb(definition), "c" * 64, now),
        )
        connection.execute(
            "UPDATE flow_runs SET active_snapshot_id = %s WHERE id = %s",
            (LEGACY_SNAPSHOT_ID, LEGACY_RUN_ID),
        )


def assert_legacy_capability_snapshot_upgraded(connection_url: str) -> None:
    with psycopg.connect(connection_url) as connection:
        version = connection.execute(
            "SELECT v.id, v.digest, b.content_hash FROM capability_versions v "
            "JOIN capability_blobs b ON b.id = v.blob_id "
            "WHERE v.source_import_id = %s AND v.source_position = 0",
            (LEGACY_IMPORT_ID,),
        ).fetchone()
        assert version is not None
        version_id, digest, content_hash = map(str, version)
        assert len(version_id) == 36
        assert len(digest) == 64
        assert content_hash == LEGACY_CONTENT_HASH
        duplicate_ref = connection.execute(
            "SELECT capability_version_id FROM node_capability_refs WHERE id = %s",
            (LEGACY_DUPLICATE_REF_ID,),
        ).fetchone()
        assert duplicate_ref is not None and str(duplicate_ref[0]) == version_id
        version_count = connection.execute(
            "SELECT count(*) FROM capability_versions WHERE digest = %s", (digest,)
        ).fetchone()
        assert version_count is not None and int(version_count[0]) == 1

        ref = connection.execute(
            "SELECT capability_version_id, normalized_config FROM node_capability_refs "
            "WHERE id = %s",
            (LEGACY_REF_ID,),
        ).fetchone()
        assert ref is not None and str(ref[0]) == version_id
        ref_config = ref[1]
        assert ref_config["capability_version_id"] == version_id
        assert ref_config["digest"] == digest
        assert ref_config["content_hash"] == LEGACY_CONTENT_HASH

        snapshot = connection.execute(
            "SELECT runtime_manifest_json, runtime_manifest_hash FROM run_snapshots WHERE id = %s",
            (LEGACY_SNAPSHOT_ID,),
        ).fetchone()
        assert snapshot is not None and len(str(snapshot[1])) == 64
        assert snapshot[0]["schema_version"] == 2
        assert snapshot[0]["openhands_version"] == "1.42.0"
        frozen = snapshot[0]["nodes"]["legacy-node"]["capabilities"][0]
        assert frozen["capability_version_id"] == version_id
        assert frozen["digest"] == digest
        assert frozen["content_hash"] == LEGACY_CONTENT_HASH
        assert frozen["runtime_config"]["capability_version_id"] == version_id
        agent_spec = snapshot[0]["nodes"]["legacy-node"]["agent_spec"]
        assert agent_spec["schema_version"] == 1
        assert agent_spec["tool_policy"]["capability_key"] == DEFAULT_TOOL_POLICY_KEY
        assert [item["name"] for item in agent_spec["tool_policy"]["runtime_config"]["tools"]] == [
            "terminal",
            "file_editor",
            "task_tracker",
        ]
        tool_policy_config = agent_spec["tool_policy"]["runtime_config"]
        assert tool_policy_config["schema_version"] == 2
        assert tool_policy_config["openhands_version"] == "1.42.0"
        assert tool_policy_config["unknown_tool"] == "DENY"
        assert tool_policy_config["tool_concurrency_limit"] == 1
        assert tool_policy_config["confirmation_required_tools"] == [
            "file_editor",
            "task_tracker",
            "terminal",
        ]
        assert agent_spec["context_policy"]["capability_key"] == DEFAULT_CONTEXT_POLICY_KEY
        context_config = agent_spec["context_policy"]["runtime_config"]
        assert context_config["load_user_skills"] is False
        assert context_config["load_public_skills"] is False
        assert context_config["load_project_skills"] is False
        assert context_config["marketplace_path"] is None
        assert context_config["registered_marketplaces"] == []
        context_policy = agent_spec["context_policy"]
        assert context_policy["capability_key"] == DEFAULT_CONTEXT_POLICY_KEY
        assert context_policy["runtime_config"]["load_user_skills"] is False
        assert context_policy["runtime_config"]["load_public_skills"] is False
        assert context_policy["runtime_config"]["load_project_skills"] is False
        assert context_policy["runtime_config"]["marketplace_path"] is None
        assert context_policy["runtime_config"]["registered_marketplaces"] == []

        policy_ref = connection.execute(
            "SELECT capability_version_id, normalized_config FROM node_capability_refs "
            "WHERE node_asset_id = %s AND capability_type = 'TOOL_POLICY'",
            (LEGACY_ASSET_ID,),
        ).fetchone()
        assert policy_ref is not None
        assert str(policy_ref[0]) == agent_spec["tool_policy"]["capability_version_id"]
        assert policy_ref[1]["digest"] == agent_spec["tool_policy"]["digest"]

        context_ref = connection.execute(
            "SELECT capability_version_id, normalized_config FROM node_capability_refs "
            "WHERE node_asset_id = %s AND capability_type = 'CONTEXT_POLICY'",
            (LEGACY_ASSET_ID,),
        ).fetchone()
        assert context_ref is not None
        assert str(context_ref[0]) == agent_spec["context_policy"]["capability_version_id"]
        assert context_ref[1]["digest"] == agent_spec["context_policy"]["digest"]

        context_ref = connection.execute(
            "SELECT capability_version_id, normalized_config FROM node_capability_refs "
            "WHERE node_asset_id = %s AND capability_type = 'CONTEXT_POLICY'",
            (LEGACY_ASSET_ID,),
        ).fetchone()
        assert context_ref is not None
        assert str(context_ref[0]) == context_policy["capability_version_id"]
        assert context_ref[1]["digest"] == context_policy["digest"]


@contextmanager
def source_database_url() -> Iterator[str]:
    configured = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if configured:
        yield configured
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image=(
            "postgres:16.9-alpine3.21"
            "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42"
        ),
        username="flowweave",
        password="flowweave_migration",
        dbname="flowweave",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


def check(source_url: str) -> None:
    connection_url = source_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parameters = psycopg.conninfo.conninfo_to_dict(connection_url)
    source_database = parameters.pop("dbname", "flowweave")
    database = f"{source_database}_migration_{uuid4().hex[:10]}"
    admin_parameters = {**parameters, "dbname": "postgres"}

    with psycopg.connect(**admin_parameters, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    target_url = source_url.rsplit("/", 1)[0] + f"/{database}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = target_url
    target_connection_url = target_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        command.upgrade(config, "head")
        assert_native_subagent_schema(target_connection_url)
        command.downgrade(config, "0005_execution")
        command.upgrade(config, "head")
        assert_native_subagent_schema(target_connection_url)
        command.downgrade(config, "0028_condensation_commands")
        insert_legacy_capability_snapshot(target_connection_url)
        command.upgrade(config, "head")
        assert_legacy_capability_snapshot_upgraded(target_connection_url)
        assert_native_subagent_schema(target_connection_url)
    finally:
        with psycopg.connect(**admin_parameters, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def main() -> None:
    with source_database_url() as source_url:
        check(source_url)


if __name__ == "__main__":
    main()
