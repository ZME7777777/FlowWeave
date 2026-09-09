"""track immutable candidate output sets for completion gates

Revision ID: 0104_candidate_output_sets
Revises: 0103_runtime_artifact_proj_idem
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0104_candidate_output_sets"
down_revision = "0103_runtime_artifact_proj_idem"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_output_sets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("completion_event_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="PENDING_REVIEW"),
        sa.Column("artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("gate_error_code", sa.String(length=80), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "attempt_id", "completion_event_id", name="uq_candidate_output_completion"
        ),
    )
    op.create_index("ix_candidate_output_sets_attempt_id", "candidate_output_sets", ["attempt_id"])
    op.create_index("ix_candidate_output_sets_status", "candidate_output_sets", ["status"])
    op.add_column(
        "node_attempts",
        sa.Column("current_candidate_output_set_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_node_attempts_current_candidate_output_set_id",
        "node_attempts",
        ["current_candidate_output_set_id"],
    )
    op.add_column(
        "gate_evaluations",
        sa.Column("candidate_output_set_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_gate_evaluations_candidate_output_set_id",
        "gate_evaluations",
        ["candidate_output_set_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gate_evaluations_candidate_output_set_id", table_name="gate_evaluations")
    op.drop_column("gate_evaluations", "candidate_output_set_id")
    op.drop_index("ix_node_attempts_current_candidate_output_set_id", table_name="node_attempts")
    op.drop_column("node_attempts", "current_candidate_output_set_id")
    op.drop_index("ix_candidate_output_sets_status", table_name="candidate_output_sets")
    op.drop_index("ix_candidate_output_sets_attempt_id", table_name="candidate_output_sets")
    op.drop_table("candidate_output_sets")
