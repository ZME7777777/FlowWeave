"""Bind direct human inputs to their declared FlowRun node.

Revision ID: 0082_node_bound_inputs
Revises: 0081_system_owned_delete
Create Date: 2026-08-31
"""

from alembic import op

revision = "0082_node_bound_inputs"
down_revision = "0081_system_owned_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``0004_artifacts`` creates its table from current ORM metadata for a
    # fresh install.  Keep this historical upgrade safe for both that path and
    # already deployed databases that need the new ownership column.
    op.execute(
        "ALTER TABLE artifact_versions "
        "ADD COLUMN IF NOT EXISTS consumer_node_key VARCHAR(100)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_artifact_versions_consumer_node_key "
        "ON artifact_versions (consumer_node_key)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_artifact_versions_consumer_node_key")
    op.execute("ALTER TABLE artifact_versions DROP COLUMN IF EXISTS consumer_node_key")
