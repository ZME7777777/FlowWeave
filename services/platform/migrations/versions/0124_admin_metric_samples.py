"""Store bounded administrator metrics history.

Revision ID: 0124_admin_metric_samples
Revises: 0123_admin_runtime_operations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0124_admin_metric_samples"
down_revision = "0123_admin_runtime_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_metric_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("metric", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_metric_samples_scope_subject_metric_observed",
        "admin_metric_samples",
        ["scope", "subject", "metric", "observed_at"],
    )
    op.create_index("ix_admin_metric_samples_observed_at", "admin_metric_samples", ["observed_at"])


def downgrade() -> None:
    op.drop_table("admin_metric_samples")
