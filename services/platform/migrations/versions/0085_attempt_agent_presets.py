"""Freeze launch-scoped Agent presets on node attempts.

Revision ID: 0085_attempt_agent_presets
Revises: 0084_attempt_context_selection
Create Date: 2026-09-01
"""

from alembic import op

revision = "0085_attempt_agent_presets"
down_revision = "0084_attempt_context_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS agent_preset_json JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE node_attempts DROP COLUMN IF EXISTS agent_preset_json")
