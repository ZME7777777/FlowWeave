"""Nest automatic records under a parent FlowRun.

Revision ID: 0087_nested_automatic_runs
Revises: 0086_run_modes_auto_drafts
Create Date: 2026-09-02
"""

from alembic import op

revision = "0087_nested_automatic_runs"
down_revision = "0086_run_modes_auto_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS parent_flow_run_id VARCHAR(36)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_flow_runs_parent_flow_run_id "
        "ON flow_runs (parent_flow_run_id)"
    )
    op.execute(
        "DO $$ BEGIN ALTER TABLE flow_runs ADD CONSTRAINT "
        "fk_flow_runs_parent_flow_run_id FOREIGN KEY (parent_flow_run_id) "
        "REFERENCES flow_runs(id) ON DELETE CASCADE; "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE flow_runs DROP CONSTRAINT IF EXISTS fk_flow_runs_parent_flow_run_id"
    )
    op.execute("DROP INDEX IF EXISTS ix_flow_runs_parent_flow_run_id")
    op.execute("ALTER TABLE flow_runs DROP COLUMN IF EXISTS parent_flow_run_id")
