"""Retiring the obsolete draft table must never discard existing records."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text


def test_revision_draft_retirement_preserves_nonempty_table(sync_session_factory):
    path = Path(__file__).parents[1] / "migrations/versions/0171_remove_flow_run_revision_drafts.py"
    spec = importlib.util.spec_from_file_location("retire_revision_drafts", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sync_session_factory.kw["bind"]
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.downgrade()
            connection.execute(text("SELECT set_config('flowweave.bypass', 'on', true)"))
            connection.execute(
                text(
                    "INSERT INTO flow_run_revision_drafts "
                    "(id, owner_user_id, flow_run_id, base_snapshot_id, base_snapshot_version, "
                    "definition_json, definition_hash, row_version, state, reason, "
                    "created_at, updated_at) "
                    "VALUES ('retained', 'owner', 'run', 'snapshot', 1, '{}', 'digest', 1, "
                    "'DRAFT', '', now(), now())"
                )
            )
            with pytest.raises(RuntimeError, match="preserve existing data"):
                migration.upgrade()
            assert (
                connection.execute(text("SELECT id FROM flow_run_revision_drafts")).scalar_one()
                == "retained"
            )
            connection.execute(text("DELETE FROM flow_run_revision_drafts"))
            migration.upgrade()
            assert not inspect(connection).has_table("flow_run_revision_drafts")
        finally:
            transaction.rollback()
