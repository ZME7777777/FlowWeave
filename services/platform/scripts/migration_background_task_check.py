"""Check the real task migration chain, ownership and later queue indexes.

Requires an isolated TEST_DATABASE_URL with CREATE DATABASE/ROLE permission.
Creates and removes only unique check databases/roles, never starts services.
Unlike earlier per-table diagnostics, this check runs the complete 0095 revision
and full head with populated tasks. --probe-head additionally migrates an empty DB.
"""

from __future__ import annotations

import argparse
import os
from uuid import uuid4

import psycopg
from alembic import command
from migration_node_run_check import config_for, probe_head
from migration_version_check import InjectedMigrationFailure, temporary_database
from psycopg import sql
from psycopg.types.json import Json
from sqlalchemy import Engine, event, make_url

BASELINE = "0005_execution"
PREVIOUS = "0094_attempt_owned_runtimes"
TARGET = "0095_user_tenancy"
HEAD = "0147_flow_run_runtime_protocol_profile"
OWNER = "00000000-0000-0000-0000-000000000001"
COLUMNS = (
    "id",
    "task_type",
    "aggregate_type",
    "aggregate_id",
    "idempotency_key",
    "payload_json",
    "state",
    "available_at",
    "lease_owner",
    "lease_until",
    "lease_generation",
    "attempts",
    "max_attempts",
    "last_error",
    "created_at",
    "updated_at",
)


def snapshot(connection):
    return connection.execute(
        f"SELECT {', '.join(COLUMNS)} FROM background_tasks ORDER BY id"
    ).fetchall()


def assert_state(url: str, revision: str, *, owned: bool, head: bool = False) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='background_tasks'"
            )
        }
        assert columns == set(COLUMNS) | ({"owner_user_id"} if owned else set())
        indexes = dict(
            connection.execute(
                "SELECT indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='background_tasks'"
            ).fetchall()
        )
        expected = {
            "background_tasks_pkey",
            "ix_background_tasks_task_type",
            "ix_background_tasks_aggregate_id",
            "ix_background_tasks_state",
            "ix_background_tasks_available_at",
            "uq_background_task_owner_key" if owned else "background_tasks_idempotency_key_key",
        }
        if owned:
            expected.add("ix_background_tasks_owner_user_id")
        if head:
            expected.update(
                {"ix_background_tasks_terminal_updated_at", "ix_background_tasks_claim_ready"}
            )
        assert set(indexes) == expected
        key = "uq_background_task_owner_key" if owned else "background_tasks_idempotency_key_key"
        assert "UNIQUE INDEX" in indexes[key]
        assert ("(owner_user_id, idempotency_key)" if owned else "(idempotency_key)") in indexes[
            key
        ]
        # The Worker delivery ledger is deliberately global and has no RLS.
        assert connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='background_tasks'::regclass"
        ).fetchone() == (False, False)
        assert connection.execute(
            "SELECT count(*) FROM pg_policies WHERE schema"
            "name='public' AND tablename='background_tasks'"
        ).fetchone() == (0,)
        if owned:
            assert connection.execute(
                "SELECT is_nullable,character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='ba"
                "ckground_tasks' AND column_name='owner_user_id'"
            ).fetchone() == ("NO", 36)
            assert connection.execute(
                "SELECT DISTINCT owner_user_id FROM background_tasks"
            ).fetchall() == [(OWNER,)]
        if head:
            assert "(state, updated_at)" in indexes["ix_background_tasks_terminal_updated_at"]
            assert "(available_at, created_at)" in indexes["ix_background_tasks_claim_ready"]
            assert all(
                value in indexes["ix_background_tasks_claim_ready"]
                for value in ("WHERE", "PENDING", "RETRY")
            )


def seed(connection):
    for number, state in ((1, "PENDING"), (2, "RUNNING")):
        connection.execute(
            "INSERT INTO background_tasks "
            "(id,task_type,aggregate_type,aggregate_id,ide"
            "mpotency_key,payload_json,state,available_at,"
            "lease_owner,lease_until,lease_generation,attempt"
            "s,max_attempts,last_error,created_at,updated_at) "
            "VALUES (%s,'MIGRATION_CHECK','CHECK',%s,%s,%s,%s,now(),%"
            "s,now()+interval '1 hour',2,1,4,'keep error',now(),now())",
            (
                str(uuid4()),
                str(uuid4()),
                "fixture-" + str(number),
                Json({"keep": True, "nested": {"number": number}}),
                state,
                "fixture-worker",
            ),
        )


def assert_global_reader(url: str) -> None:
    role = sql.Identifier("task_check_" + uuid4().hex)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(role))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT ON background_tasks TO {}").format(role))
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
                for owner in (OWNER, str(uuid4())):
                    connection.execute("SELECT set_config('flowweave.user_id',%s,true)", (owner,))
                    assert connection.execute(
                        "SELECT count(*) FROM background_tasks"
                    ).fetchone() == (2,)
        finally:
            connection.execute(sql.SQL("REVOKE SELECT ON background_tasks FROM {}").format(role))
            connection.execute(sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def assert_idempotency(url: str, before):
    with psycopg.connect(url) as connection:
        try:
            with connection.transaction():
                try:
                    with connection.transaction():
                        connection.execute(
                            "INSERT INTO background_tasks "
                            "SELECT %s,task_type,aggregate_type,aggreg"
                            "ate_id,idempotency_key,payload_json,state,"
                            "available_at,lease_owner,lease_until,lease_"
                            "generation,attempts,max_attempts,last_error,"
                            "created_at,updated_at,owner_user_id FR"
                            "OM background_tasks ORDER BY id LIMIT 1",
                            (str(uuid4()),),
                        )
                except psycopg.errors.UniqueViolation:
                    pass
                else:
                    raise AssertionError("same owner accepted duplicate task key")
                connection.execute(
                    "INSERT INTO background_tasks "
                    "SELECT %s,task_type,aggregate_type,aggreg"
                    "ate_id,idempotency_key,payload_json,state,"
                    "available_at,lease_owner,lease_until,lease_"
                    "generation,attempts,max_attempts,last_error,"
                    "created_at,updated_at,%s FROM background_tasks ORDER BY id LIMIT 1",
                    (str(uuid4()), str(uuid4())),
                )
                raise InjectedMigrationFailure
        except InjectedMigrationFailure:
            pass
        assert snapshot(connection) == before


def check(source: str) -> list[str]:
    completed = []
    with temporary_database(source) as target:
        url = make_url(target).set(drivername="postgresql").render_as_string(hide_password=False)
        config = config_for(target)
        command.upgrade(config, "0004_artifacts")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('background_tasks') IS NOT NULL"
                ).scalar_one()
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_version_write)
        try:
            try:
                command.upgrade(config, BASELINE)
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("0005 failure injection did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_version_write)
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0004_artifacts",)
            ]
            for table in ("background_tasks", "run_events", "gate_evaluations"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
        completed.append("0005_failure_rolls_back_all_execution_tables")
        command.upgrade(config, BASELINE)
        assert_state(url, BASELINE, owned=False)
        completed.append("empty_0005_frozen_background_task_schema")
        command.downgrade(config, "0004_artifacts")
        command.upgrade(config, BASELINE)
        assert_state(url, BASELINE, owned=False)
        completed.append("0005_downgrade_upgrade")
        with psycopg.connect(url) as connection:
            seed(connection)
            before = snapshot(connection)
            for column in ("attempts", "lease_generation"):
                try:
                    with connection.transaction():
                        connection.execute(f"UPDATE background_tasks SET {column}=-1")
                except psycopg.errors.CheckViolation:
                    pass
                else:
                    raise AssertionError("negative counter accepted")
        completed.append("task_lease_and_attempt_checks_reject_negative_values")
        command.upgrade(config, PREVIOUS)
        assert_state(url, PREVIOUS, owned=False)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        completed.append("real_chain_to_0094_preserves_all_task_fields")

        def fail_tenant_version(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT bool_and(owner_user_id IS NOT NULL) FROM background_tasks"
                ).scalar_one()
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('uq_background_task_owner_key') IS NOT NULL"
                ).scalar_one()
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_tenant_version)
        try:
            try:
                command.upgrade(config, TARGET)
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("0095 failure injection did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_tenant_version)
        assert_state(url, PREVIOUS, owned=False)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
            assert connection.execute("SELECT to_regclass('users')").fetchone() == (None,)
        completed.append("full_0095_failure_rolls_back_users_owner_and_unique_conversion")
        command.upgrade(config, TARGET)
        assert_state(url, TARGET, owned=True)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        completed.append("full_0094_to_0095_retry_preserves_tasks_and_backfills_owner")
        assert_idempotency(url, before)
        completed.append("full_0095_task_idempotency_scoped_per_owner")
        assert_global_reader(url)
        completed.append("worker_task_ledger_globally_readable_without_rls")
        command.downgrade(config, PREVIOUS)
        assert_state(url, PREVIOUS, owned=False)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        command.upgrade(config, TARGET)
        assert_state(url, TARGET, owned=True)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        completed.append("full_0095_downgrade_upgrade_preserves_original_tasks")
        command.upgrade(config, "head")
        assert_state(url, HEAD, owned=True, head=True)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        completed.append("populated_tasks_to_head_preserve_rows_and_add_later_queue_indexes")
        command.upgrade(config, "head")
        assert_state(url, HEAD, owned=True, head=True)
        with psycopg.connect(url) as connection:
            assert snapshot(connection) == before
        completed.append("current_head_upgrade_preserves_task_snapshot")
    return completed


def main():
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
