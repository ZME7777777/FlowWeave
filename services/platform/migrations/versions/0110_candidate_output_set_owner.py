"""restore ownership column for candidate output sets

Revision ID: 0110_candidate_output_set_owner
Revises: 0109_hook_capabilities
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0110_candidate_output_set_owner"
down_revision = "0109_hook_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_output_sets",
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        "UPDATE candidate_output_sets AS candidate "
        "SET owner_user_id = flow_run.owner_user_id "
        "FROM node_attempts AS attempt "
        "JOIN node_runs AS node_run ON node_run.id = attempt.node_run_id "
        "JOIN flow_runs AS flow_run ON flow_run.id = node_run.flow_run_id "
        "WHERE candidate.attempt_id = attempt.id"
    )
    op.alter_column("candidate_output_sets", "owner_user_id", nullable=False)
    op.create_index(
        "ix_candidate_output_sets_owner_user_id",
        "candidate_output_sets",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_output_sets_owner_user_id", table_name="candidate_output_sets")
    op.drop_column("candidate_output_sets", "owner_user_id")
