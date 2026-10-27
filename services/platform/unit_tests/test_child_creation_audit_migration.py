"""Migration protects legacy lineage and refuses partial/lossy governance audit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer


def test_creation_audit_migration_preserves_legacy_and_refuses_partial_or_lossy_data(monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0179_agent_child_creation_audit.py"
    spec = importlib.util.spec_from_file_location("child_creation_audit_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with PostgresContainer(
        "postgres:16.9-alpine3.21"
        "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42",
        driver="psycopg",
    ) as postgres:
        engine = create_engine(postgres.get_connection_url())
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE agent_child_conversations (id TEXT PRIMARY KEY)"))
                conn.execute(text("INSERT INTO agent_child_conversations VALUES ('legacy')"))
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
                migration.upgrade()
                assert (
                    conn.execute(
                        text("SELECT creation_policy_version FROM agent_child_conversations")
                    ).scalar_one()
                    is None
                )
            columns = (
                "creation_policy_version",
                "request_digest",
                "expected_agent_digest",
                "expected_persistence_dir",
            )
            good = ["binding-isolated-v1", "a" * 64, "b" * 64, "/state/child"]
            for missing in range(4):
                with pytest.raises(DBAPIError):
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "INSERT INTO agent_child_conversations "
                                "VALUES ('partial', :p, :r, :a, :d)"
                            ),
                            dict(
                                zip(
                                    ("p", "r", "a", "d"),
                                    [
                                        None if i == missing else value
                                        for i, value in enumerate(good)
                                    ],
                                    strict=True,
                                )
                            ),
                        )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO agent_child_conversations VALUES ('governed', :p, :r, :a, :d)"
                    ),
                    dict(zip(("p", "r", "a", "d"), good, strict=True)),
                )
            with pytest.raises(DBAPIError, match="Cannot discard governed child creation audit"):
                with engine.begin() as conn:
                    monkeypatch.setattr(
                        migration, "op", Operations(MigrationContext.configure(conn))
                    )
                    migration.downgrade()
            with engine.begin() as conn:
                assert (
                    conn.execute(
                        text("SELECT COUNT(*) FROM agent_child_conversations")
                    ).scalar_one()
                    == 2
                )
                conn.execute(text("DELETE FROM agent_child_conversations WHERE id = 'governed'"))
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
                migration.downgrade()
                assert (
                    conn.execute(text("SELECT id FROM agent_child_conversations")).scalar_one()
                    == "legacy"
                )
                assert not set(columns) & {
                    c["name"] for c in inspect(conn).get_columns("agent_child_conversations")
                }
        finally:
            engine.dispose()
