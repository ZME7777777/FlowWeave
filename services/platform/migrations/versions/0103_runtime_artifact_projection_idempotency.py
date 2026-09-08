"""make Runtime Artifact completion projections idempotent

Revision ID: 0103_runtime_artifact_projection_idempotency
Revises: 0102_event_trigger_observers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0103_runtime_artifact_projection_idempotency"
down_revision = "0102_event_trigger_observers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifact_versions",
        sa.Column("runtime_completion_event_id", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_artifact_versions_runtime_completion_event_id",
        "artifact_versions",
        ["runtime_completion_event_id"],
    )
    op.create_unique_constraint(
        "uq_runtime_artifact_completion",
        "artifact_versions",
        ["producer_attempt_id", "field_key", "runtime_completion_event_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_runtime_artifact_completion", "artifact_versions", type_="unique")
    op.drop_index(
        "ix_artifact_versions_runtime_completion_event_id", table_name="artifact_versions"
    )
    op.drop_column("artifact_versions", "runtime_completion_event_id")
