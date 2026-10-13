"""Check frozen 0005 gate audit records and the native 0095 ownership contract.

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

BASELINE = "0005_execution"
PREVIOUS = "0094_attempt_owned_runtimes"
TABLE = "gate_evaluations"
ORIGINAL_COLUMNS = (
    "id",
    "attempt_id",
    "policy_snapshot_key",
    "stage",
    "policy_position",
    "evaluation_attempt",
    "state",
    "decision",
    "input_hash",
    "result_json",
    "log_excerpt",
    "error_code",
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
                "WHERE table_schema='public' AND table_name='gate_evaluations'"
            )
        }
        assert columns == set(ORIGINAL_COLUMNS) | ({"owner_user_id"} if owned else set())
        indexes = dict(
            connection.execute(
                "SELECT indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='gate_evaluations'"
            ).fetchall()
        )
        assert set(indexes) == {
            "gate_evaluations_pkey",
            "uq_gate_evaluation_attempt",
            "ix_gate_evaluations_attempt_id",
        } | ({"ix_gate_evaluations_owner_user_id"} if owned else set())
        assert "UNIQUE INDEX" in indexes["uq_gate_evaluation_attempt"]
        fks = connection.execute(
            "SELECT confrelid::regclass::text,confdeltype FROM pg_constraint "
            "WHERE conrelid='gate_evaluations'::regclass AND contype='f'"
        ).fetchall()
        assert sorted(fks) == ([("node_attempts", "c")] if revision == BASELINE else [])
        assert connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='gate_evaluations'::regclass"
        ).fetchone() == (owned, owned)
        policies = connection.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='public' "
            "AND tablename='gate_evaluations'"
        ).fetchall()
        assert policies == ([("pl_tenant_gate_evaluations",)] if owned else [])
        if owned:
            assert connection.execute(
                "SELECT is_nullable,character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='gate_evaluations' "
                "AND column_name='owner_user_id'"
            ).fetchone() == ("NO", 36)
            assert connection.execute(
                "SELECT DISTINCT owner_user_id FROM gate_evaluations"
            ).fetchall() == [(LEGACY_OWNER,)]


def rows(connection):
    return connection.execute(
        f"SELECT {', '.join(ORIGINAL_COLUMNS)} FROM gate_evaluations ORDER BY id"
    ).fetchall()


def seed(connection) -> None:
    from migration_artifact_check import seed as seed_artifact

    seed_artifact(connection)
    attempt = connection.execute("SELECT id FROM node_attempts LIMIT 1").fetchone()[0]
    for number, decision, error in ((1, "FAIL", "FIXTURE_FAILURE"), (2, "PASS", None)):
        connection.execute(
            "INSERT INTO gate_evaluations "
            "(id,attempt_id,policy_snapshot_key,stage,policy_position,evaluation_attempt,"
            "state,decision,input_hash,result_json,log_excerpt,error_code,created_at) "
            "VALUES (%s,%s,'policy','START',4,%s,'COMPLETED',%s,%s,%s,'keep audit',%s,now())",
            (
                str(uuid4()),
                attempt,
                number,
                decision,
                "a" * 64,
                Json({"keep": True, "attempt": number}),
                error,
            ),
        )


def assert_visibility(url: str) -> None:
    role = sql.Identifier("gate_check_" + uuid4().hex)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(role))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT ON gate_evaluations TO {}").format(role))
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
                for owner, expected in ((LEGACY_OWNER, 2), (str(uuid4()), 0)):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    assert connection.execute(
                        "SELECT count(*) FROM gate_evaluations"
                    ).fetchone() == (expected,)
                connection.execute("SELECT set_config('flowweave.bypass','on',true)")
                assert connection.execute("SELECT count(*) FROM gate_evaluations").fetchone() == (
                    2,
                )
        finally:
            connection.execute(sql.SQL("REVOKE SELECT ON gate_evaluations FROM {}").format(role))
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def check(source_url: str) -> list[str]:
    completed = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, "0004_artifacts")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('gate_evaluations') IS NOT NULL"
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
                ("0004_artifacts",)
            ]
            assert connection.execute("SELECT to_regclass('gate_evaluations')").fetchone() == (
                None,
            )
        completed.append("0005_failure_rolls_back_table_and_revision")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("empty_0005_frozen_gate_evaluation_schema")
        command.downgrade(config, "0004_artifacts")
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE)
        completed.append("0005_downgrade_upgrade")
        command.upgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS)
        with psycopg.connect(url) as connection:
            seed(connection)
            before = rows(connection)
        completed.append("real_chain_to_0094_defers_gate_owner_and_candidate_output")
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
                        "SELECT bool_and(owner_user_id IS NOT NULL) FROM gate_evaluations"
                    ).scalar_one()
                    raise InjectedMigrationFailure
            except InjectedMigrationFailure:
                pass
            assert_schema(url, PREVIOUS)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_gate_failure_rolls_back_backfill_and_rls")
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    revision["_add_owner"](TABLE)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("native_0095_gate_retry_preserves_all_fields_and_assigns_owner")
            assert_visibility(url)
            completed.append("native_0095_gate_rls_isolates_owners_and_allows_bypass")
            command.upgrade(config, PREVIOUS)
            assert_schema(url, PREVIOUS, owned=True)
            with psycopg.connect(url) as connection:
                assert rows(connection) == before
            completed.append("already_applied_0005_not_replayed")
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
