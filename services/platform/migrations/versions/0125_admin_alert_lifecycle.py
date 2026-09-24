"""Store administrator alert lifecycle state and audit actions.

Revision ID: 0125_admin_alert_lifecycle
Revises: 0124_admin_metric_samples
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0125_admin_alert_lifecycle"
down_revision = "0124_admin_metric_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_alert_states",
        sa.Column("alert_key", sa.String(length=300), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_user_id", sa.String(length=36)),
        sa.Column("acknowledged_by_username", sa.String(length=80)),
        sa.Column("silenced_until", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(length=500)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("alert_key"),
    )
    op.create_index(
        "ix_admin_alert_states_silenced_until", "admin_alert_states", ["silenced_until"]
    )
    op.create_table(
        "admin_alert_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_key", sa.String(length=300), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("actor_username", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("silenced_until", sa.DateTime(timezone=True)),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('ACKNOWLEDGE', 'SILENCE')", name="ck_admin_alert_action"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_alert_actions_alert_key", "admin_alert_actions", ["alert_key"])
    op.create_index(
        "ix_admin_alert_actions_actor_user_id", "admin_alert_actions", ["actor_user_id"]
    )
    op.create_index("ix_admin_alert_actions_request_id", "admin_alert_actions", ["request_id"])
    op.create_index("ix_admin_alert_actions_created_at", "admin_alert_actions", ["created_at"])


def downgrade() -> None:
    op.drop_table("admin_alert_actions")
    op.drop_table("admin_alert_states")
