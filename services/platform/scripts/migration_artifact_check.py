"""Check frozen 0004 tables and the native 0095 Artifact ownership DDL.

Requires an isolated TEST_DATABASE_URL with CREATE DATABASE permission. Only
unique temporary databases are migrated and removed; no services are started.
The ownership contract check invokes the unmodified 0095 helpers on two tables,
without recording 0095. --probe-head executes the real complete migration chain
and propagates independent blockers in other tables.
"""

from __future__ import annotations

import argparse
import os
import runpy
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from migration_node_run_check import config_for, probe_head
from migration_version_check import InjectedMigrationFailure, temporary_database
from psycopg import sql
from psycopg.types.json import Json
from sqlalchemy import Engine, create_engine, event, make_url

BASELINE = "0004_artifacts"
PREVIOUS = "0094_attempt_owned_runtimes"
TABLES = ("artifact_versions", "attempt_input_bindings")
ORIGINAL_COLUMNS = {
    "artifact_versions": (
        "id",
        "flow_run_id",
        "producer_attempt_id",
        "field_key",
        "version_no",
        "artifact_type",
        "storage_key",
        "uri",
        "inline_content",
        "content_hash",
        "byte_size",
        "mime_type",
        "source",
        "metadata_json",
        "created_at",
    ),
    "attempt_input_bindings": (
        "id",
        "attempt_id",
        "input_field_key",
        "artifact_version_id",
        "binding_source",
    ),
}
LEGACY_OWNER = "00000000-0000-0000-0000-000000000001"


def columns(connection, table: str) -> set[str]:
    return {
        r[0]
        for r in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
    }


def assert_schema(url: str, revision: str, *, baseline: bool = False, owned: bool = False) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        for table in TABLES:
            expected = set(ORIGINAL_COLUMNS[table])
            if not baseline and table == "artifact_versions":
                expected.add("consumer_node_key")
            if owned:
                expected.add("owner_user_id")
            assert columns(connection, table) == expected
        unique_columns = connection.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='artifact_versions'::regclass AND conname='uq_artifact_field_version'"
        ).fetchone()[0]
        assert unique_columns == "UNIQUE (flow_run_id, field_key, version_no)"
        indexes = {
            r[0]
            for r in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=ANY(%s)",
                (list(TABLES),),
            )
        }
        assert {
            "artifact_versions_pkey",
            "uq_artifact_field_version",
            "ix_artifact_versions_flow_run_id",
            "ix_artifact_versions_producer_attempt_id",
            "attempt_input_bindings_pkey",
            "uq_attempt_input_field",
            "ix_attempt_input_bindings_attempt_id",
        } <= indexes
        fks = connection.execute(
            "SELECT conrelid::regclass::text, confrelid::regclass::text, confdeltype "
            "FROM pg_constraint WHERE conrelid=ANY(ARRAY['artifact_versions'::regclass,"
            "'attempt_input_bindings'::regclass]) AND contype='f'"
        ).fetchall()
        original_fks = {
            ("artifact_versions", "flow_runs", "c"),
            ("artifact_versions", "node_attempts", "r"),
            ("attempt_input_bindings", "node_attempts", "c"),
            ("attempt_input_bindings", "artifact_versions", "r"),
        }
        # 0088 intentionally removes business foreign keys on the real chain.
        assert set(fks) == (original_fks if baseline else set())
        for table in TABLES:
            assert (f"ix_{table}_owner_user_id" in indexes) is owned
            assert connection.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
                (table,),
            ).fetchone() == (owned, owned)
            policies = connection.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=%s",
                (table,),
            ).fetchall()
            assert policies == ([(f"pl_tenant_{table}",)] if owned else [])
            if owned:
                assert connection.execute(
                    "SELECT is_nullable,character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s AND column_name='owner_user_id'",
                    (table,),
                ).fetchone() == ("NO", 36)
                assert connection.execute(
                    f"SELECT DISTINCT owner_user_id FROM {table}"
                ).fetchall() == [(LEGACY_OWNER,)]


def rows(connection) -> list:
    return [
        connection.execute(
            f"SELECT {', '.join(ORIGINAL_COLUMNS[table])} FROM {table} ORDER BY id"
        ).fetchall()
        for table in TABLES
    ]


def assert_owner_visibility(url: str) -> None:
    role = f"artifact_check_{uuid4().hex}"
    identifier = sql.Identifier(role)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(identifier)
        )
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(identifier))
            connection.execute(
                sql.SQL("GRANT SELECT ON artifact_versions,attempt_input_bindings TO {}").format(
                    identifier
                )
            )
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(identifier))
                for owner, expected in ((LEGACY_OWNER, 1), (str(uuid4()), 0)):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    for table in TABLES:
                        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (
                            expected,
                        )
                connection.execute("SELECT set_config('flowweave.bypass','on',true)")
                for table in TABLES:
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
        finally:
            connection.execute(
                sql.SQL("REVOKE SELECT ON artifact_versions,attempt_input_bindings FROM {}").format(
                    identifier
                )
            )
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(identifier))
            connection.execute(sql.SQL("DROP ROLE {}").format(identifier))


def seed(connection) -> None:
    flow, run, node, snapshot, attempt, artifact, binding = [str(uuid4()) for _ in range(7)]
    connection.execute(
        "INSERT INTO flow_definitions "
        "(id,name,description,row_version,created_at,updated_at) "
        "VALUES (%s,'artifact fixture','keep',1,now(),now())",
        (flow,),
    )
    connection.execute(
        "INSERT INTO flow_runs (id,flow_definition_id,run_no,name,state,row_version,started_at) "
        "VALUES (%s,%s,1,'fixture','ACTIVE',1,now())",
        (run, flow),
    )
    connection.execute(
        "INSERT INTO node_runs "
        "(id,flow_run_id,flow_node_snapshot_key,sequence_no,state,created_from,activated_at) "
        "VALUES (%s,%s,'node',1,'ACTIVE','HUMAN_START',now())",
        (node, run),
    )
    connection.execute(
        "INSERT INTO run_snapshots "
        "(id,flow_run_id,version,schema_version,definition_json,definition_hash,"
        "runtime_manifest_json,runtime_manifest_hash,created_at) "
        "VALUES (%s,%s,1,1,'{}',%s,'{}',%s,now())",
        (snapshot, run, "d" * 64, "m" * 64),
    )
    connection.execute(
        "INSERT INTO node_attempts "
        "(id,node_run_id,attempt_no,snapshot_id,state,state_version,startup_mode,"
        "output_targets_json,created_at,updated_at) "
        "VALUES (%s,%s,1,%s,'WAITING_INPUT',1,'PROMPT','{}',now(),now())",
        (attempt, node, snapshot),
    )
    connection.execute(
        "INSERT INTO artifact_versions "
        "(id,flow_run_id,producer_attempt_id,field_key,version_no,artifact_type,"
        "inline_content,content_hash,byte_size,mime_type,source,metadata_json,created_at) "
        "VALUES (%s,%s,%s,'output',1,'TEXT','keep me',%s,7,'text/plain','HUMAN',%s,now())",
        (artifact, run, attempt, "a" * 64, Json({"keep": True})),
    )
    connection.execute(
        "INSERT INTO attempt_input_bindings "
        "(id,attempt_id,input_field_key,artifact_version_id,binding_source) "
        "VALUES (%s,%s,'input',%s,'HUMAN')",
        (binding, attempt, artifact),
    )


def check(source_url: str) -> list[str]:
    completed: list[str] = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, "0003_runs")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('attempt_input_bindings') IS NOT NULL"
                ).scalar_one()
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_version_write)
        try:
            try:
                command.upgrade(config, BASELINE)
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("failure injection did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_version_write)
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0003_runs",)
            ]
            for table in TABLES:
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
        completed.append("0004_failure_rolls_back_both_tables_and_revision")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE, baseline=True)
        completed.append("empty_0004_frozen_artifact_and_binding_schema")
        command.downgrade(config, "0003_runs")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE, baseline=True)
        completed.append("0004_downgrade_upgrade")
        command.upgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            seed(connection)
            before = rows(connection)
            connection.execute("UPDATE artifact_versions SET consumer_node_key='node'")
        completed.append("real_chain_to_0094_defers_owner_and_runtime_fields")
        revision = runpy.run_path(
            str(Path(__file__).parents[1] / "migrations/versions/0095_user_tenancy.py")
        )
        engine = create_engine(target_url)
        try:
            try:
                with engine.begin() as connection:
                    with Operations.context(MigrationContext.configure(connection)):
                        for table in TABLES:
                            revision["_add_owner"](table)
                    assert connection.exec_driver_sql(
                        "SELECT bool_and(owner_user_id IS NOT NULL) FROM artifact_versions"
                    ).scalar_one()
                    raise InjectedMigrationFailure
            except InjectedMigrationFailure:
                pass
            assert_schema(url, PREVIOUS)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append(
                "native_0095_artifact_ddl_failure_rolls_back_owner_backfill_and_policies"
            )
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    for table in TABLES:
                        revision["_add_owner"](table)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
                assert connection.execute(
                    "SELECT consumer_node_key FROM artifact_versions"
                ).fetchall() == [("node",)]
            completed.append("native_0095_artifact_ddl_retry_backfills_owner_and_enforces_rls")
            assert_owner_visibility(url)
            completed.append(
                "native_0095_artifact_rls_isolates_two_owners_and_allows_control_bypass"
            )
            command.upgrade(config, PREVIOUS)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("already_applied_0004_not_replayed_and_owned_rows_preserved")
        finally:
            engine.dispose()
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-head", action="store_true")
    args = parser.parse_args()
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        raise RuntimeError("Set TEST_DATABASE_URL to an isolated PostgreSQL test server")
    for scenario in check(source_url):
        print("PASS " + scenario)
    if args.probe_head:
        probe_head(source_url)
        print("PASS empty_database_to_head")


if __name__ == "__main__":
    main()
