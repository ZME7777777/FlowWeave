"""add durable event trigger delivery intents

Revision ID: 0101_event_trigger_deliveries
Revises: 0100_event_trigger_versions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0101_event_trigger_deliveries"
down_revision = "0100_event_trigger_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_trigger_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trigger_version_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_action_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'RETRY', 'DEAD')",
            name="ck_event_trigger_delivery_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_event_trigger_delivery_attempts_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_event_trigger_delivery_owner_key"
        ),
    )
    for name, column in (
        ("trigger_version_id", "trigger_version_id"),
        ("trigger_action_id", "trigger_action_id"),
        ("event_id", "event_id"),
        ("flow_run_id", "flow_run_id"),
        ("node_run_id", "node_run_id"),
        ("attempt_id", "attempt_id"),
        ("state", "state"),
        ("available_at", "available_at"),
        ("owner_user_id", "owner_user_id"),
    ):
        op.create_index(f"ix_event_trigger_deliveries_{name}", "event_trigger_deliveries", [column])


def downgrade() -> None:
    for name in (
        "owner_user_id",
        "available_at",
        "state",
        "attempt_id",
        "node_run_id",
        "flow_run_id",
        "event_id",
        "trigger_action_id",
        "trigger_version_id",
    ):
        op.drop_index(f"ix_event_trigger_deliveries_{name}", table_name="event_trigger_deliveries")
    op.drop_table("event_trigger_deliveries")
