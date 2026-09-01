"""Add explicit run modes and automatic-run draft configuration.

Revision ID: 0086_run_modes_auto_drafts
Revises: 0085_attempt_agent_presets
Create Date: 2026-09-01
"""

from alembic import op

revision = "0086_run_modes_auto_drafts"
down_revision = "0085_attempt_agent_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS run_mode VARCHAR(20) "
        "NOT NULL DEFAULT 'MANUAL'"
    )
    op.execute("ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS automation_plan_json JSON")
    op.execute("CREATE INDEX IF NOT EXISTS ix_flow_runs_run_mode ON flow_runs (run_mode)")
    op.execute(
        "DO $$ BEGIN "
        "ALTER TABLE flow_runs ADD CONSTRAINT ck_flow_runs_run_mode "
        "CHECK (run_mode IN ('MANUAL', 'AUTOMATIC')); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE flow_runs DROP CONSTRAINT IF EXISTS ck_flow_runs_run_mode")
    op.execute("DROP INDEX IF EXISTS ix_flow_runs_run_mode")
    op.execute("ALTER TABLE flow_runs DROP COLUMN IF EXISTS automation_plan_json")
    op.execute("ALTER TABLE flow_runs DROP COLUMN IF EXISTS run_mode")
