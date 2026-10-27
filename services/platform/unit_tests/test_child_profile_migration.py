from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer


def test_v3_migration_preserves_old_profiles_and_refuses_lossy_downgrade(monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0175_add_oh_fw_v3_protocol_profile.py"
    spec = importlib.util.spec_from_file_location("child_profile_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    columns = (
        (
            "environment_versions",
            "runtime_protocol_profile",
            "ck_environment_version_runtime_protocol_profile",
        ),
        ("flow_run_runtimes", "protocol_profile", "ck_flow_run_runtime_protocol_profile"),
        ("runtime_generations", "protocol_profile", "ck_runtime_generation_protocol_profile"),
    )
    with PostgresContainer(
        "postgres:16.9-alpine3.21"
        "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42",
        driver="psycopg",
    ) as postgres:
        engine = create_engine(postgres.get_connection_url())
        try:
            with engine.begin() as conn:
                for table, column, constraint in columns:
                    conn.execute(
                        text(
                            f"CREATE TABLE {table} ({column} TEXT NOT NULL CONSTRAINT {constraint} "
                            f"CHECK ({column} IN ('OH_FW_V1', 'OH_FW_V2')))"
                        )
                    )
                    conn.execute(text(f"INSERT INTO {table} VALUES ('OH_FW_V1'), ('OH_FW_V2')"))
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
                migration.upgrade()
            # Each table independently prevents downgrade. The exception rolls
            # back the DDL as well; no historical row is rewritten.
            for target, target_column, _ in columns:
                with engine.begin() as conn:
                    conn.execute(text(f"INSERT INTO {target} VALUES ('OH_FW_V3')"))
                with pytest.raises(DBAPIError, match="Cannot downgrade"):
                    with engine.begin() as conn:
                        monkeypatch.setattr(
                            migration, "op", Operations(MigrationContext.configure(conn))
                        )
                        migration.downgrade()
                with engine.begin() as conn:
                    assert conn.execute(text(f"SELECT COUNT(*) FROM {target}")).scalar_one() == 3
                    conn.execute(text(f"DELETE FROM {target} WHERE {target_column} = 'OH_FW_V3'"))
            with engine.begin() as conn:
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
                migration.downgrade()
                for table, column, _ in columns:
                    assert conn.execute(
                        text(f"SELECT {column} FROM {table} ORDER BY 1")
                    ).scalars().all() == ["OH_FW_V1", "OH_FW_V2"]
            with pytest.raises(DBAPIError):
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO flow_run_runtimes VALUES ('OH_FW_V3')"))
        finally:
            engine.dispose()
