"""Freeze runtime gate policies on each node attempt.

Revision ID: 0083_attempt_gate_policies
Revises: 0082_node_bound_inputs
Create Date: 2026-08-31
"""

from alembic import op

revision = "0083_attempt_gate_policies"
down_revision = "0082_node_bound_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS "
        "gate_policies_json JSON NOT NULL DEFAULT '[]'::json"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE node_attempts DROP COLUMN IF EXISTS gate_policies_json")
