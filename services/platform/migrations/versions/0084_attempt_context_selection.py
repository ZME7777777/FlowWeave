"""Freeze human-selected node Context on each attempt.

Revision ID: 0084_attempt_context_selection
Revises: 0083_attempt_gate_policies
Create Date: 2026-08-31
"""

from alembic import op

revision = "0084_attempt_context_selection"
down_revision = "0083_attempt_gate_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS context_ids_json JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE node_attempts DROP COLUMN IF EXISTS context_ids_json")
