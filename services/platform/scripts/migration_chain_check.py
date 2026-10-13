"""Verify full historical migrations and preservation of mixed audit fixtures.

Requires isolated TEST_DATABASE_URL with CREATE DATABASE/ROLE permission.
Uses only unique disposable databases/roles. No production copies or services.
Runs complete revisions, not per-table DDL or stamps.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from migration_artifact_check import seed as seed_artifacts
from migration_background_task_check import seed as seed_tasks
from migration_capability_import_check import seed as seed_imports
from migration_node_run_check import config_for, probe_head
from migration_version_check import InjectedMigrationFailure, temporary_database
from psycopg import sql
from psycopg.types.json import Json
from sqlalchemy import Engine, event, make_url

PREVIOUS = "0094_attempt_owned_runtimes"
TARGET = "0095_user_tenancy"
HEAD = "0146_shared_flow_runs"
OWNER = "00000000-0000-0000-0000-000000000001"
TABLES = (
    "flow_definitions",
    "flow_runs",
    "run_snapshots",
    "node_runs",
    "node_attempts",
    "node_assets",
    "flow_nodes",
    "flow_edges",
    "flow_port_mappings",
    "gate_policies",
    "artifact_versions",
    "attempt_input_bindings",
    "human_actions",
    "run_events",
    "gate_evaluations",
    "capability_imports",
    "background_tasks",
)


def frozen_columns(connection):
    return {
        table: tuple(
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (table,),
            )
        )
        for table in TABLES
    }


def snapshots(connection, columns):
    result = {}
    for table, names in columns.items():
        key = "cursor" if table == "run_events" else "id"
        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
            sql.SQL(",").join(map(sql.Identifier, names)),
            sql.Identifier(table),
            sql.Identifier(key),
        )
        result[table] = connection.execute(query).fetchall()
    return result


def schema_snapshot(connection):
    result = {}
    for table in TABLES:
        columns = connection.execute(
            "SELECT column_name,data_type,character_maximum_length,is_nullable,column_default "
            "FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        indexes = connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename=%s ORDER BY indexname",
            (table,),
        ).fetchall()
        constraints = connection.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid=%s::regclass ORDER BY conname",
            (table,),
        ).fetchall()
        rls = connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
            (table,),
        ).fetchone()
        policies = connection.execute(
            "SELECT policyname,qual,with_check FROM pg_policies "
            "WHERE schemaname='public' AND tablename=%s ORDER BY policyname",
            (table,),
        ).fetchall()
        result[table] = (columns, indexes, constraints, rls, policies)
    return result


def seed(connection):
    seed_artifacts(connection)
    flow, source, target, asset = [str(uuid4()) for _ in range(4)]
    connection.execute(
        "INSERT INTO flow_definitions (id,name,description,row_version,created_at,updated_at) "
        "VALUES (%s,'mixed edge fixture','keep',1,now(),now())",
        (flow,),
    )
    connection.execute(
        "INSERT INTO node_assets "
        "(id,name,description,icon_kind,icon_value,row_version,created_at,updated_at) "
        "VALUES (%s,'mixed fixture asset','keep','TEXT','N',1,now(),now())",
        (asset,),
    )
    for ident, key in ((source, "source"), (target, "target")):
        connection.execute(
            "INSERT INTO flow_nodes "
            "(id,flow_id,instance_key,node_asset_id,position_x,position_y,config_override) "
            "VALUES (%s,%s,%s,%s,0,0,'{}')",
            (ident, flow, key, asset),
        )
    for position in (2, 7):
        connection.execute(
            "INSERT INTO flow_edges "
            "(id,flow_id,source_flow_node_id,target_flow_node_id,position) VALUES (%s,%s,%s,%s,%s)",
            (str(uuid4()), flow, source, target, position),
        )
    seed_imports(connection)
    seed_tasks(connection)
    run = connection.execute("SELECT id FROM flow_runs LIMIT 1").fetchone()[0]
    attempt, node_run = connection.execute(
        "SELECT id,node_run_id FROM node_attempts LIMIT 1"
    ).fetchone()
    flow, source, target = connection.execute(
        "SELECT flow_id,source_flow_node_id,target_flow_node_id FROM flow_edges LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE flow_nodes SET alias=instance_key||'-alia"
        "s',position_x=13,position_y=-7,config_override=%s",
        (Json({"keep": True, "nested": {"number": 3}}),),
    )
    for number in (1, 2):
        connection.execute(
            "INSERT INTO flow_port_mappings "
            "(id,flow_id,source_flow_node_id,source_outpu"
            "t_key,target_flow_node_id,target_input_key) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (str(uuid4()), flow, source, "output-" + str(number), target, "input-" + str(number)),
        )
        connection.execute(
            "INSERT INTO gate_policies "
            "(id,flow_node_id,stage,position,gate_type,e"
            "nabled,timeout_seconds,config,content_hash) "
            "VALUES (%s,%s,'START',%s,'SCRIPT',%s,23,%s,%s)",
            (str(uuid4()), source, number, number == 1, Json({"keep": True}), "p" * 64),
        )
        connection.execute(
            "INSERT INTO human_actions "
            "(id,flow_run_id,node_run_id,attempt_id,action_"
            "type,idempotency_key,payload_json,created_at) "
            "VALUES (%s,%s,%s,%s,'CONFIRM_START',%s,%s,now())",
            (
                str(uuid4()),
                run,
                node_run,
                attempt,
                "audit-" + str(number),
                Json({"number": number}),
            ),
        )
        connection.execute(
            "INSERT INTO run_events (flow_run_id,node_run_id,"
            "attempt_id,event_type,payload_json,occurred_at) "
            "VALUES (%s,%s,%s,%s,%s,now())",
            (
                run,
                node_run,
                attempt,
                "ATTEMPT_STARTED" if number == 1 else "ATTEMPT_COMPLETED",
                Json({"number": number}),
            ),
        )
        connection.execute(
            "INSERT INTO gate_evaluations "
            "(id,attempt_id,policy_snapshot_key,stage,poli"
            "cy_position,evaluation_attempt,state,decision,"
            "input_hash,result_json,log_excerpt,error_code,created_at) "
            "VALUES (%s,%s,'policy','START',4,%s,'COMPLETED',%s,%s,%s,'keep audit',%s,now())",
            (
                str(uuid4()),
                attempt,
                number,
                "FAIL" if number == 1 else "PASS",
                "i" * 64,
                Json({"number": number}),
                "FIXTURE_FAILURE" if number == 1 else None,
            ),
        )


def assert_revision(url, expected):
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (expected,)
        ]


def assert_tenant_projection(url, counts):
    role = sql.Identifier("chain_check_" + uuid4().hex)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(role))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            for table in TABLES:
                connection.execute(
                    sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), role)
                )
                state = connection.execute(
                    "SELECT relrowsecurity,relforcerowsecuri"
                    "ty FROM pg_class WHERE oid=%s::regclass",
                    (table,),
                ).fetchone()
                assert state == ((False, False) if table == "background_tasks" else (True, True))
                assert connection.execute(
                    sql.SQL("SELECT DISTINCT owner_user_id FROM {}").format(sql.Identifier(table))
                ).fetchall() == [(OWNER,)]
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
                for owner in (OWNER, str(uuid4())):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    for table in TABLES:
                        expected = (
                            counts[table] if owner == OWNER or table == "background_tasks" else 0
                        )
                        assert connection.execute(
                            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                        ).fetchone() == (expected,)
                connection.execute("SELECT set_config('flowweave.bypass','on',true)")
                for table in TABLES:
                    assert connection.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone() == (counts[table],)
        finally:
            for table in TABLES:
                connection.execute(
                    sql.SQL("REVOKE SELECT ON {} FROM {}").format(sql.Identifier(table), role)
                )
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def check(source):
    completed = []
    with temporary_database(source) as target:
        url = make_url(target).set(drivername="postgresql").render_as_string(hide_password=False)
        config = config_for(target)
        command.upgrade(config, PREVIOUS)
        with psycopg.connect(url) as connection:
            seed(connection)
            columns = frozen_columns(connection)
            before = snapshots(connection, columns)
            before_schema = schema_snapshot(connection)
            assert all(before[table] for table in TABLES)
        counts = {table: len(rows) for table, rows in before.items()}
        completed.append("mixed_fixture_all_17_business_and_audit_tables_populated")

        def fail_version(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('uq_background_task_owner_key') IS NOT NULL"
                ).scalar_one()
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_version)
        try:
            try:
                command.upgrade(config, TARGET)
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("full revision failure did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_version)
        assert_revision(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
            assert connection.execute("SELECT to_regclass('users')").fetchone() == (None,)
            assert frozen_columns(connection) == columns
            assert schema_snapshot(connection) == before_schema
        completed.append("full_0095_failure_restores_all_17_table_rows_and_schema")
        command.upgrade(config, TARGET)
        assert_revision(url, TARGET)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
        assert_tenant_projection(url, counts)
        completed.append("full_0095_retry_preserves_every_original_field_and_tenant_projection")
        command.downgrade(config, PREVIOUS)
        assert_revision(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
            assert frozen_columns(connection) == columns
            assert schema_snapshot(connection) == before_schema
        command.upgrade(config, TARGET)
        assert_revision(url, TARGET)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
        completed.append("full_0095_downgrade_upgrade_preserves_mixed_audit_records")
        command.upgrade(config, "head")
        assert_revision(url, HEAD)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
            assert connection.execute(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name='alembic_version' AND column_name='version_num'"
            ).fetchone() == (128,)
        completed.append(
            "mixed_fixture_to_head_preserves_all_17_table_fields_and_long_revision_capacity"
        )
        command.upgrade(config, "head")
        assert_revision(url, HEAD)
        with psycopg.connect(url) as connection:
            assert snapshots(connection, columns) == before
        completed.append("current_head_upgrade_preserves_complete_mixed_fixture")
    probe_head(source)
    completed.append("independent_empty_database_full_chain_to_head")
    return completed


def main():
    source = os.environ.get("TEST_DATABASE_URL")
    if not source:
        raise RuntimeError("Set TEST_DATABASE_URL to an isolated PostgreSQL test server")
    for scenario in check(source):
        print("PASS " + scenario)


if __name__ == "__main__":
    main()
