"""Check frozen 0003 human audit records and the native 0095 ownership contract.

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
from psycopg.types.json import Json
from sqlalchemy import Engine, create_engine, event, make_url

BASELINE = "0003_runs"
PREVIOUS = "0094_attempt_owned_runtimes"
TABLE = "human_actions"
ORIGINAL_COLUMNS = (
    "id",
    "flow_run_id",
    "node_run_id",
    "attempt_id",
    "action_type",
    "idempotency_key",
    "payload_json",
    "created_at",
)
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
                "WHERE table_schema='public' AND table_name='human_actions'"
            )
        }
        assert columns == set(ORIGINAL_COLUMNS) | ({"owner_user_id"} if owned else set())
        indexes = dict(
            connection.execute(
                "SELECT indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='human_actions'"
            ).fetchall()
        )
        assert set(indexes) == {
            "human_actions_pkey",
            *(("uq_human_action_owner_key",) if owned else ("human_actions_idempotency_key_key",)),
            "ix_human_actions_flow_run_id",
            "ix_human_actions_node_run_id",
            "ix_human_actions_attempt_id",
        } | ({"ix_human_actions_owner_user_id"} if owned else set())
        assert (
            "UNIQUE INDEX"
            in indexes[
                "uq_human_action_owner_key" if owned else "human_actions_idempotency_key_key"
            ]
        )
        fks = connection.execute(
            "SELECT confrelid::regclass::text,confdeltype FROM pg_constraint "
            "WHERE conrelid='human_actions'::regclass AND contype='f'"
        ).fetchall()
        assert sorted(fks) == (
            [("flow_runs", "c"), ("node_attempts", "c"), ("node_runs", "c")]
            if revision == BASELINE
            else []
        )
        assert connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='human_actions'::regclass"
        ).fetchone() == (owned, owned)
        policies = connection.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='public' "
            "AND tablename='human_actions'"
        ).fetchall()
        assert policies == ([("pl_tenant_human_actions",)] if owned else [])
        if owned:
            assert connection.execute(
                "SELECT is_nullable,character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='human_actions' "
                "AND column_name='owner_user_id'"
            ).fetchone() == ("NO", 36)
            assert connection.execute(
                "SELECT DISTINCT owner_user_id FROM human_actions"
            ).fetchall() == [(LEGACY_OWNER,)]


def rows(connection):
    return connection.execute(
        f"SELECT {', '.join(ORIGINAL_COLUMNS)} FROM human_actions ORDER BY id"
    ).fetchall()


def seed(connection) -> None:
    from migration_artifact_check import seed as seed_artifact

    seed_artifact(connection)
    run = connection.execute("SELECT id FROM flow_runs LIMIT 1").fetchone()[0]
    attempt, node = connection.execute(
        "SELECT id,node_run_id FROM node_attempts LIMIT 1"
    ).fetchone()
    for number in (1, 2):
        connection.execute(
            "INSERT INTO human_actions "
            "(id,flow_run_id,node_run_id,attempt_id,action_type,"
            "idempotency_key,payload_json,created_at) "
            "VALUES (%s,%s,%s,%s,'CONFIRM_START',%s,%s,now())",
            (
                str(uuid4()),
                run,
                node,
                attempt,
                "fixture-" + str(number),
                Json({"keep": True, "number": number}),
            ),
        )


def scope_unique(revision) -> None:
    # Exercise the unmodified loop on the exact HumanAction metadata entry,
    # without recording 0095 or involving independently blocked tables.
    function = revision["_scope_business_uniques"]
    metadata = function.__globals__["_OWNER_UNIQUES"]
    try:
        function.__globals__["_OWNER_UNIQUES"] = tuple(
            row for row in metadata if row[0] == "human_actions"
        )
        function()
    finally:
        function.__globals__["_OWNER_UNIQUES"] = metadata


def assert_visibility(url: str) -> None:
    role = sql.Identifier("action_check_" + uuid4().hex)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(role))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT ON human_actions TO {}").format(role))
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
                for owner, expected in ((LEGACY_OWNER, 2), (str(uuid4()), 0)):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    assert connection.execute("SELECT count(*) FROM human_actions").fetchone() == (
                        expected,
                    )
                connection.execute("SELECT set_config('flowweave.bypass','on',true)")
                assert connection.execute("SELECT count(*) FROM human_actions").fetchone() == (2,)
        finally:
            connection.execute(sql.SQL("REVOKE SELECT ON human_actions FROM {}").format(role))
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def check(source_url: str) -> list[str]:
    completed = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, "0002_flows")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('human_actions') IS NOT NULL"
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
                ("0002_flows",)
            ]
            assert connection.execute("SELECT to_regclass('human_actions')").fetchone() == (None,)
        completed.append("0003_failure_rolls_back_table_and_revision")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("empty_0003_frozen_human_action_schema")
        command.downgrade(config, "0002_flows")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("0003_downgrade_upgrade")
        command.upgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            seed(connection)
            before = rows(connection)
        completed.append("real_chain_to_0094_preserves_legacy_human_action_key")
        revision = runpy.run_path(
            str(Path(__file__).parents[1] / "migrations/versions/0095_user_tenancy.py")
        )
        engine = create_engine(target_url)
        try:
            try:
                with engine.begin() as connection:
                    with Operations.context(MigrationContext.configure(connection)):
                        revision["_add_owner"](TABLE)
                        scope_unique(revision)
                    assert connection.exec_driver_sql(
                        "SELECT bool_and(owner_user_id IS NOT NULL) FROM human_actions"
                    ).scalar_one()
                    raise InjectedMigrationFailure
            except InjectedMigrationFailure:
                pass
            assert_schema(url, PREVIOUS)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_action_failure_rolls_back_backfill_and_rls")
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    revision["_add_owner"](TABLE)
                    scope_unique(revision)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_action_retry_preserves_all_fields_and_assigns_owner")
            assert_visibility(url)
            completed.append("native_0095_action_rls_isolates_owners_and_allows_bypass")
            with psycopg.connect(url) as connection:
                try:
                    with connection.transaction():
                        try:
                            with connection.transaction():
                                connection.execute(
                                    "INSERT INTO human_actions "
                                    "SELECT %s,flow_run_id,node_run_id,attempt_id,action_type,"
                                    "idempotency_key,payload_json,created_at,owner_user_id "
                                    "FROM human_actions ORDER BY id LIMIT 1",
                                    (str(uuid4()),),
                                )
                        except psycopg.errors.UniqueViolation:
                            pass
                        else:
                            raise AssertionError("same owner accepted duplicate key")
                        connection.execute(
                            "INSERT INTO human_actions "
                            "SELECT %s,flow_run_id,node_run_id,attempt_id,action_type,"
                            "idempotency_key,payload_json,created_at,%s "
                            "FROM human_actions ORDER BY id LIMIT 1",
                            (str(uuid4()), str(uuid4())),
                        )
                        raise InjectedMigrationFailure
                except InjectedMigrationFailure:
                    pass
                assert rows(connection) == before
            completed.append("native_0095_human_idempotency_scoped_per_owner")
            command.upgrade(config, PREVIOUS)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("already_applied_0003_not_replayed")
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
