"""Check long Alembic revisions in a disposable PostgreSQL database.

Requires TEST_DATABASE_URL; never starts local services or uses DATABASE_URL.
Only the uniquely named database created here is migrated and removed.
--metadata-only isolates the actual 0138 migration behind a no-op 0137 fixture;
it checks Alembic metadata behavior, not the complete historical schema chain.
"""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Engine, event, make_url
from sqlalchemy.exc import OperationalError

PREVIOUS = "0137_conversation_message_order"
HEAD = "0138_admin_resource_cleanup_operations"
AUDIT_TABLE = "admin_resource_cleanup_operations"


class InjectedMigrationFailure(Exception):
    """Fail after DDL, just before Alembic records the new revision."""


@contextmanager
def migration_config(*, metadata_only: bool) -> Iterator[Config]:
    platform = Path(__file__).parents[1]
    config = Config(str(platform / "alembic.ini"))
    if not metadata_only:
        yield config
        return

    temporary_root = Path(
        os.environ.get("MIGRATION_CHECK_TMP_ROOT", str(platform.parents[1] / ".tmp"))
    )
    temporary_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="migration-version-", dir=temporary_root) as directory:
        fixture = Path(directory)
        (fixture / "versions").mkdir()
        shutil.copyfile(platform / "migrations/env.py", fixture / "env.py")
        shutil.copyfile(
            platform / f"migrations/versions/{HEAD}.py",
            fixture / f"versions/{HEAD}.py",
        )
        # Alembic itself creates its default version table and records both
        # revisions. No stamp, private Alembic APIs or revision rewrites.
        (fixture / "versions/0137_fixture.py").write_text(
            f"revision = {PREVIOUS!r}\n"
            "down_revision = None\n"
            "def upgrade(): pass\n"
            "def downgrade(): pass\n"
        )
        config.set_main_option("script_location", str(fixture))
        yield config


@contextmanager
def temporary_database(source_url: str) -> Iterator[str]:
    source = make_url(source_url)
    if source.drivername != "postgresql+psycopg":
        raise ValueError("TEST_DATABASE_URL must use postgresql+psycopg")
    database = f"flowweave_revision_check_{uuid4().hex}"
    admin_url = source.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield source.set(database=database).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def assert_state(url: str, revision: str, capacity: int | None, *, audit: bool) -> None:
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        assert connection.execute(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version' "
            "AND column_name = 'version_num'"
        ).fetchone() == (capacity,)
        row = connection.execute("SELECT to_regclass(%s)", (AUDIT_TABLE,)).fetchone()
        assert row is not None and (row[0] is not None) == audit


def check(source_url: str, *, metadata_only: bool = False) -> list[str]:
    completed: list[str] = []
    with (
        temporary_database(source_url) as target_url,
        migration_config(metadata_only=metadata_only) as config,
    ):
        url = (
            make_url(target_url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        config.attributes["database_url"] = target_url
        command.upgrade(config, "head")
        assert_state(url, HEAD, 128, audit=True)
        completed.append("empty_metadata_to_head" if metadata_only else "empty_database_to_head")

        # Downgrade keeps the metadata capacity: old revision values fit, and
        # retrying a later upgrade must not need an operational prerequisite.
        command.downgrade(config, PREVIOUS)
        assert_state(url, PREVIOUS, 128, audit=False)
        completed.append("downgrade_preserves_capacity")

        with psycopg.connect(url) as connection:
            connection.execute(
                "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(32)"
            )
        assert_state(url, PREVIOUS, 32, audit=False)

        def fail_version_write(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE alembic_version SET version_num="):
                assert (
                    connection.exec_driver_sql(
                        "SELECT character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'alembic_version' "
                        "AND column_name = 'version_num'"
                    ).scalar_one()
                    == 128
                )
                assert (
                    connection.exec_driver_sql(
                        "SELECT to_regclass('admin_resource_cleanup_operations')"
                    ).scalar_one()
                    is not None
                )
                raise InjectedMigrationFailure

        event.listen(Engine, "before_cursor_execute", fail_version_write)
        try:
            try:
                command.upgrade(config, "head")
            except InjectedMigrationFailure:
                pass
            else:
                raise AssertionError("failure injection did not run")
        finally:
            event.remove(Engine, "before_cursor_execute", fail_version_write)
        assert_state(url, PREVIOUS, 32, audit=False)
        completed.append("version_write_failure_rolls_back_ddl_and_capacity")

        # EXCLUSIVE permits the bridge's ordinary SELECT but blocks ALTER.
        with psycopg.connect(url) as blocker:
            blocker.execute("LOCK TABLE alembic_version IN EXCLUSIVE MODE")
            started = monotonic()
            try:
                command.upgrade(config, "head")
            except OperationalError as error:
                assert error.orig.sqlstate == "55P03"
                assert 4.5 <= monotonic() - started < 15
            else:
                raise AssertionError("metadata ALTER did not respect lock_timeout")
        assert_state(url, PREVIOUS, 32, audit=False)
        completed.append("lock_timeout_rolls_back_and_is_bounded")

        def configure_timeout(connection, record):
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '17s'")
            connection.commit()

        timeouts: list[str] = []

        def observe_timeout(connection, cursor, statement, parameters, context, executemany):
            if "DECLARE previous_lock_timeout" in statement:
                timeouts.append(connection.exec_driver_sql("SHOW lock_timeout").scalar_one())

        event.listen(Engine, "connect", configure_timeout)
        event.listen(Engine, "after_cursor_execute", observe_timeout)
        try:
            command.upgrade(config, "head")
        finally:
            event.remove(Engine, "connect", configure_timeout)
            event.remove(Engine, "after_cursor_execute", observe_timeout)
        assert timeouts == ["17s"]
        assert_state(url, HEAD, 128, audit=True)
        completed.append("legacy_32_upgrade_retry_and_timeout_restoration")

        # Include the already-widened rollout database, larger/custom columns,
        # and unbounded types. Never shrink operators' existing metadata.
        for column_type, capacity in [("VARCHAR(128)", 128), ("VARCHAR(256)", 256), ("TEXT", None)]:
            command.downgrade(config, PREVIOUS)
            with psycopg.connect(url) as connection:
                connection.execute(
                    sql.SQL("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE {} ").format(
                        sql.SQL(column_type)
                    )
                )
            command.upgrade(config, "head")
            assert_state(url, HEAD, capacity, audit=True)
            command.upgrade(config, "head")
            assert_state(url, HEAD, capacity, audit=True)
            completed.append(f"existing_{column_type.lower()}_and_current_head")
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        raise RuntimeError("Set TEST_DATABASE_URL to an isolated PostgreSQL test server")
    for scenario in check(source_url, metadata_only=args.metadata_only):
        print(f"PASS {scenario}")


if __name__ == "__main__":
    main()
