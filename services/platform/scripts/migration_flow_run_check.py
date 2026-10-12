"""Check the frozen 0003 FlowRun schema and real 0093 scheduling migration.

Requires an isolated TEST_DATABASE_URL with CREATE DATABASE permission.
Only unique temporary databases are migrated and removed; no local services
are started. --probe-head reports later independent migration failures.
"""

from __future__ import annotations

import argparse
import os
from uuid import uuid4

import psycopg
from alembic import command
from migration_node_run_check import config_for, probe_head
from migration_version_check import InjectedMigrationFailure, temporary_database
from psycopg.types.json import Json
from sqlalchemy import Engine, event, make_url

BASELINE = "0003_runs"
PREVIOUS = "0092_node_run_names"
TARGET = "0093_flow_run_schedules"
ORIGINAL_COLUMNS = (
    "id",
    "flow_definition_id",
    "run_no",
    "name",
    "state",
    "active_snapshot_id",
    "row_version",
    "completion_mode",
    "started_at",
    "finished_at",
)
LATER_COLUMNS = {
    "lark_folder_token",
    "lark_folder_url",
    "environment_version_id",
    "run_mode",
    "automation_plan_json",
    "parent_flow_run_id",
}
SCHEDULE_COLUMNS = {"schedule_id", "schedule_occurrence_id"}
SCHEDULE_TABLES = ("flow_run_schedules", "flow_run_schedule_occurrences")
PRESERVED_COLUMNS = ORIGINAL_COLUMNS + tuple(sorted(LATER_COLUMNS))


def assert_schema(url: str, revision: str, *, scheduled: bool, baseline: bool = False) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        columns = dict(
            connection.execute(
                "SELECT column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='flow_runs'"
            ).fetchall()
        )
        expected = set(ORIGINAL_COLUMNS) | (set() if baseline else LATER_COLUMNS)
        assert set(columns) == expected | (SCHEDULE_COLUMNS if scheduled else set())
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='flow_runs'"
            )
        }
        assert {
            "flow_runs_pkey",
            "uq_flow_run_number",
            "ix_flow_runs_flow_definition_id",
        } <= indexes
        for name in SCHEDULE_COLUMNS:
            assert (f"ix_flow_runs_{name}" in indexes) is scheduled
            if scheduled:
                assert columns[name] == 36
                assert connection.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='flow_runs' AND column_name=%s",
                    (name,),
                ).fetchone() == ("YES",)
        for table in SCHEDULE_TABLES:
            assert (
                connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None
            ) is scheduled
        if baseline:
            assert connection.execute(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid='flow_runs'::regclass AND contype='f'"
            ).fetchall() == [("r",)]


def original_row(connection, record_id: str):
    # Include every pre-0093 column, not only identity fields.
    return connection.execute(
        f"SELECT {', '.join(PRESERVED_COLUMNS)} FROM flow_runs WHERE id=%s", (record_id,)
    ).fetchone()


def check(source_url: str) -> list[str]:
    completed: list[str] = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE, scheduled=False, baseline=True)
        completed.append("empty_0003_frozen_flow_run_schema")
        command.downgrade(config, "0002_flows")
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT to_regclass('flow_runs')").fetchone() == (None,)
        command.upgrade(config, BASELINE)
        assert_schema(url, BASELINE, scheduled=False, baseline=True)
        completed.append("0003_downgrade_upgrade")

        command.upgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS, scheduled=False)
        record_id, flow_id = str(uuid4()), str(uuid4())
        with psycopg.connect(url) as connection:
            connection.execute(
                "INSERT INTO flow_definitions "
                "(id,name,description,row_version,created_at,updated_at) "
                "VALUES (%s,'migration fixture','preserve me',1,now(),now())",
                (flow_id,),
            )
            connection.execute(
                "INSERT INTO flow_runs "
                "(id,flow_definition_id,run_no,name,state,active_snapshot_id,row_version,"
                "completion_mode,started_at,finished_at,run_mode) "
                "VALUES (%s,%s,1,'keep me','ACTIVE',NULL,3,NULL,now(),NULL,'MANUAL')",
                (record_id, flow_id),
            )
            connection.execute(
                "UPDATE flow_runs SET lark_folder_token='keep-token', "
                "lark_folder_url='https://example.invalid/keep', "
                "automation_plan_json=%s WHERE id=%s",
                (Json({"keep": True}), record_id),
            )
            before = original_row(connection, record_id)
        assert before is not None
        completed.append("real_chain_to_0092_without_scheduling_fields")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT to_regclass('flow_run_schedules') IS NOT NULL "
                    "AND to_regclass('flow_run_schedule_occurrences') IS NOT NULL"
                ).scalar_one()
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_version_write)
        try:
            try:
                command.upgrade(config, TARGET)
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("version write failure did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_version_write)
        assert_schema(url, PREVIOUS, scheduled=False)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
        completed.append("0093_failure_rolls_back_columns_indexes_tables_and_preserves_run")

        command.upgrade(config, TARGET)
        assert_schema(url, TARGET, scheduled=True)
        schedule_id, occurrence_id = str(uuid4()), str(uuid4())
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
            assert connection.execute(
                "SELECT schedule_id,schedule_occurrence_id FROM flow_runs WHERE id=%s", (record_id,)
            ).fetchone() == (None, None)
            connection.execute(
                "INSERT INTO flow_run_schedules "
                "(id,flow_definition_id,environment_version_id,name,run_mode,start_node_key,"
                "plan_json,interval_minutes,status,row_version,created_at,updated_at) "
                "VALUES (%s,%s,%s,'keep schedule','MANUAL','node','{}',5,'ACTIVE',1,now(),now())",
                (schedule_id, flow_id, str(uuid4())),
            )
            connection.execute(
                "INSERT INTO flow_run_schedule_occurrences "
                "(id,schedule_id,trigger_kind,state,flow_run_id,created_at,updated_at) "
                "VALUES (%s,%s,'MANUAL','PENDING',%s,now(),now())",
                (occurrence_id, schedule_id, record_id),
            )
            connection.execute(
                "UPDATE flow_runs SET schedule_id=%s,schedule_occurrence_id=%s WHERE id=%s",
                (schedule_id, occurrence_id, record_id),
            )
            snapshots = [
                connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("flow_runs", *SCHEDULE_TABLES)
            ]
        completed.append("0092_to_0093_retry_preserves_run_and_adds_nullable_locators")

        command.upgrade(config, TARGET)
        assert_schema(url, TARGET, scheduled=True)
        with psycopg.connect(url) as connection:
            assert [
                connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("flow_runs", *SCHEDULE_TABLES)
            ] == snapshots
        completed.append("current_0093_preserves_run_schedule_and_occurrence")

        command.downgrade(config, PREVIOUS)
        assert_schema(url, PREVIOUS, scheduled=False)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
        command.upgrade(config, TARGET)
        assert_schema(url, TARGET, scheduled=True)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
        completed.append("0093_downgrade_upgrade_preserves_original_run")
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-head", action="store_true")
    args = parser.parse_args()
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        raise RuntimeError("Set TEST_DATABASE_URL to an isolated PostgreSQL test server")
    for scenario in check(source_url):
        print(f"PASS {scenario}")
    if args.probe_head:
        probe_head(source_url)
        print("PASS empty_database_to_head")


if __name__ == "__main__":
    main()
