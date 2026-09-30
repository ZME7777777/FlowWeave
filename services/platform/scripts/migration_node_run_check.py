"""Check the real 0003/0092 NodeRun migration chain in a temporary database.

Requires an isolated TEST_DATABASE_URL with CREATE DATABASE permission.
Never starts local services. --probe-head additionally checks an empty DB
through the complete chain; any later historical failure remains an error.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from migration_version_check import InjectedMigrationFailure, temporary_database
from sqlalchemy import Engine, event, make_url

BASELINE = "0003_runs"
PREVIOUS = "0091_skill_collection_latest"
TARGET = "0092_node_run_names"
ORIGINAL_COLUMNS = (
    "id",
    "flow_run_id",
    "flow_node_snapshot_key",
    "sequence_no",
    "state",
    "accepted_attempt_id",
    "created_from",
    "activated_at",
)


def config_for(url: str) -> Config:
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


def assert_node_run(url: str, revision: str, *, named: bool) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        columns = dict(
            connection.execute(
                "SELECT column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'node_runs'"
            ).fetchall()
        )
        assert set(columns) == set(ORIGINAL_COLUMNS) | ({"name"} if named else set())
        if named:
            assert columns["name"] == 220
            assert connection.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'node_runs' "
                "AND column_name = 'name'"
            ).fetchone() == ("YES",)
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'node_runs'"
            )
        }
        assert {"node_runs_pkey", "uq_run_node_sequence", "ix_node_runs_flow_run_id"} <= indexes


def original_row(connection, record_id: str):
    # The unchanged fields include the exact timestamp and every identity.
    return connection.execute(
        f"SELECT {', '.join(ORIGINAL_COLUMNS)} FROM node_runs WHERE id = %s", (record_id,)
    ).fetchone()


def check(source_url: str) -> list[str]:
    completed: list[str] = []
    with temporary_database(source_url) as target_url:
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config = config_for(target_url)
        command.upgrade(config, BASELINE)
        assert_node_run(url, BASELINE, named=False)
        completed.append("empty_0003_frozen_node_run_schema")
        command.downgrade(config, "0002_flows")
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT to_regclass('node_runs')").fetchone() == (None,)
        command.upgrade(config, BASELINE)
        assert_node_run(url, BASELINE, named=False)
        completed.append("0003_downgrade_upgrade")

        command.upgrade(config, PREVIOUS)
        assert_node_run(url, PREVIOUS, named=False)
        record_id = str(uuid4())
        with psycopg.connect(url) as connection:
            connection.execute(
                "INSERT INTO node_runs "
                "(id, flow_run_id, flow_node_snapshot_key, sequence_no, state, "
                "accepted_attempt_id, created_from, activated_at) "
                "VALUES (%s, %s, 'node', 1, 'ACTIVE', NULL, 'HUMAN_START', now())",
                (record_id, str(uuid4())),
            )
            before = original_row(connection, record_id)
        assert before is not None
        completed.append("real_chain_to_0091_without_future_column")

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert connection.exec_driver_sql(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'node_runs' "
                    "AND column_name = 'name')"
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
        assert_node_run(url, PREVIOUS, named=False)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
        completed.append("0092_failure_rolls_back_column_and_preserves_row")

        command.upgrade(config, TARGET)
        assert_node_run(url, TARGET, named=True)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
            assert connection.execute(
                "SELECT name FROM node_runs WHERE id = %s", (record_id,)
            ).fetchone() == (None,)
            connection.execute("UPDATE node_runs SET name = 'keep me' WHERE id = %s", (record_id,))
        completed.append("0091_to_0092_retry_preserves_row_and_adds_nullable_name")

        command.upgrade(config, TARGET)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
            assert connection.execute(
                "SELECT name FROM node_runs WHERE id = %s", (record_id,)
            ).fetchone() == ("keep me",)
        completed.append("current_0092_keeps_name_and_original_fields")

        command.downgrade(config, PREVIOUS)
        assert_node_run(url, PREVIOUS, named=False)
        command.upgrade(config, TARGET)
        assert_node_run(url, TARGET, named=True)
        with psycopg.connect(url) as connection:
            assert original_row(connection, record_id) == before
        completed.append("0092_downgrade_upgrade_preserves_original_fields")
    return completed


def probe_head(source_url: str) -> None:
    with temporary_database(source_url) as target_url:
        command.upgrade(config_for(target_url), "head")


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
