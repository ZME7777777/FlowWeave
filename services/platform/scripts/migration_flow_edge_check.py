"""Check frozen 0002 control edges and the native 0095 ownership contract.

Use an isolated TEST_DATABASE_URL with CREATE DATABASE permission. Only unique
check databases and roles are created and removed; no local services start.
The native helper check does not record 0095; --probe-head runs the complete
real chain and propagates independent blockers.
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
from sqlalchemy import Engine, create_engine, event, make_url

BASELINE = "0002_flows"
PREVIOUS = "0094_attempt_owned_runtimes"
TABLE = "flow_edges"
ORIGINAL_COLUMNS = ("id", "flow_id", "source_flow_node_id", "target_flow_node_id", "position")
LEGACY_OWNER = "00000000-0000-0000-0000-000000000001"


def assert_schema(url: str, revision: str, *, owned: bool = False) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        columns = {
            r[0]
            for r in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='flow_edges'"
            )
        }
        assert columns == set(ORIGINAL_COLUMNS) | ({"owner_user_id"} if owned else set())
        indexes = dict(
            connection.execute(
                "SELECT indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='flow_edges'"
            ).fetchall()
        )
        assert set(indexes) == {
            "flow_edges_pkey",
            "uq_flow_edge_position",
            "ix_flow_edges_flow_id",
        } | ({"ix_flow_edges_owner_user_id"} if owned else set())
        assert "UNIQUE INDEX" in indexes["uq_flow_edge_position"]
        fks = connection.execute(
            "SELECT confrelid::regclass::text,confdeltype FROM pg_constraint "
            "WHERE conrelid='flow_edges'::regclass AND contype='f'"
        ).fetchall()
        assert sorted(fks) == (
            [("flow_definitions", "c"), ("flow_nodes", "c"), ("flow_nodes", "c")]
            if revision == BASELINE
            else []
        )
        assert connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='flow_edges'::regclass"
        ).fetchone() == (owned, owned)
        policies = connection.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='public' "
            "AND tablename='flow_edges'"
        ).fetchall()
        assert policies == ([("pl_tenant_flow_edges",)] if owned else [])
        if owned:
            assert connection.execute(
                "SELECT is_nullable,character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='flow_edges' "
                "AND column_name='owner_user_id'"
            ).fetchone() == ("NO", 36)
            assert connection.execute(
                "SELECT DISTINCT owner_user_id FROM flow_edges"
            ).fetchall() == [(LEGACY_OWNER,)]


def rows(connection):
    return connection.execute(
        f"SELECT {', '.join(ORIGINAL_COLUMNS)} FROM flow_edges ORDER BY id"
    ).fetchall()


def seed(connection) -> None:
    flow, source, target = [str(uuid4()) for _ in range(3)]
    connection.execute(
        "INSERT INTO flow_definitions "
        "(id,name,description,lark_root_folder_url,row_version,created_at,updated_at) "
        "VALUES (%s,'edge fixture','keep','https://example.invalid/root',1,now(),now())",
        (flow,),
    )
    asset = str(uuid4())
    connection.execute(
        "INSERT INTO node_assets "
        "(id,name,description,icon_kind,icon_value,row_version,created_at,updated_at) "
        "VALUES (%s,'edge fixture asset','keep','TEXT','N',1,now(),now())",
        (asset,),
    )
    for ident, key in ((source, "source"), (target, "target")):
        connection.execute(
            "INSERT INTO flow_nodes (id,flow_id,instance_key,node_asset_id,position_x,position_y,"
            "config_override) VALUES (%s,%s,%s,%s,0,0,'{}')",
            (ident, flow, key, asset),
        )
    for position in (2, 7):
        connection.execute(
            "INSERT INTO flow_edges (id,flow_id,source_flow_node_id,target_flow_node_id,position) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid4()), flow, source, target, position),
        )


def assert_visibility(url: str) -> None:
    role = sql.Identifier("edge_check_" + uuid4().hex)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(role))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT ON flow_edges TO {}").format(role))
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
                for owner, expected in ((LEGACY_OWNER, 2), (str(uuid4()), 0)):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    assert connection.execute("SELECT count(*) FROM flow_edges").fetchone() == (
                        expected,
                    )
                connection.execute("SELECT set_config('flowweave.bypass','on',true)")
                assert connection.execute("SELECT count(*) FROM flow_edges").fetchone() == (2,)
        finally:
            connection.execute(sql.SQL("REVOKE SELECT ON flow_edges FROM {}").format(role))
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def check(source_url: str) -> list[str]:
    completed = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, "0001_catalog")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('flow_edges') IS NOT NULL"
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
                ("0001_catalog",)
            ]
            assert connection.execute("SELECT to_regclass('flow_edges')").fetchone() == (None,)
        completed.append("0002_failure_rolls_back_table_and_revision")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("empty_0002_frozen_control_edge_schema")
        command.downgrade(config, "0001_catalog")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("0002_downgrade_upgrade")
        with psycopg.connect(url) as connection:
            seed(connection)
            before = rows(connection)
        command.upgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            assert rows(connection) == before
        completed.append("real_chain_to_0094_preserves_control_edge_identity_and_position")
        revision = runpy.run_path(
            str(Path(__file__).parents[1] / "migrations/versions/0095_user_tenancy.py")
        )
        engine = create_engine(target_url)
        try:
            try:
                with engine.begin() as connection:
                    with Operations.context(MigrationContext.configure(connection)):
                        revision["_add_owner"](TABLE)
                    assert connection.exec_driver_sql(
                        "SELECT bool_and(owner_user_id IS NOT NULL) FROM flow_edges"
                    ).scalar_one()
                    raise InjectedMigrationFailure
            except InjectedMigrationFailure:
                pass
            assert_schema(url, PREVIOUS)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_edge_failure_rolls_back_backfill_and_rls")
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    revision["_add_owner"](TABLE)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_edge_retry_preserves_all_fields_and_assigns_owner")
            assert_visibility(url)
            completed.append("native_0095_edge_rls_isolates_owners_and_allows_bypass")
            command.upgrade(config, PREVIOUS)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("already_applied_0002_not_replayed")
        finally:
            engine.dispose()
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-head", action="store_true")
    args = parser.parse_args()
    source = os.environ.get("TEST_DATABASE_URL")
    if not source:
        raise RuntimeError("Set TEST_DATABASE_URL to an isolated PostgreSQL test server")
    for scenario in check(source):
        print("PASS " + scenario)
    if args.probe_head:
        probe_head(source)
        print("PASS empty_database_to_head")


if __name__ == "__main__":
    main()
