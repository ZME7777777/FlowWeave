"""Store sanitized formal Runtime business observations.

Revision ID: 0129_runtime_business_observations
Revises: 0128_admin_runtime_isolation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129_runtime_business_observations"
down_revision = "0128_admin_runtime_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_business_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("runtime_kind", sa.String(length=30), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("representative_binding_id", sa.String(length=36), nullable=True),
        sa.Column("impacted_bindings", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=True),
        sa.Column("readiness_status", sa.String(length=80), nullable=True),
        sa.Column("runtime_availability", sa.String(length=40), nullable=True),
        sa.Column("stages_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "runtime_kind IN ('FLOW_RUN', 'AGENT_WORKSPACE')",
            name="ck_runtime_business_observation_kind",
        ),
        sa.CheckConstraint(
            "status IN ('OK', 'DEGRADED', 'NO_ACTIVE_CONVERSATION')",
            name="ck_runtime_business_observation_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_business_observation_kind", "runtime_business_observations", ["runtime_kind"]
    )
    op.create_index(
        "ix_runtime_business_observation_session",
        "runtime_business_observations",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_runtime_business_observation_observed", "runtime_business_observations", ["observed_at"]
    )


def downgrade() -> None:
    op.drop_table("runtime_business_observations")
