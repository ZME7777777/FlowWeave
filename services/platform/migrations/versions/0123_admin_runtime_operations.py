"""Audit administrator Runtime replacement submissions.

Revision ID: 0123_admin_runtime_operations
Revises: 0122_agent_conversation_unread
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0123_admin_runtime_operations"
down_revision = "0122_agent_conversation_unread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_runtime_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("actor_username", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=36), nullable=False),
        sa.Column("expected_generation", sa.Integer(), nullable=False),
        sa.Column("expected_session_row_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action = 'REPLACE_RUNTIME'", name="ck_admin_runtime_operation_action"),
        sa.CheckConstraint(
            "expected_generation >= 1", name="ck_admin_runtime_operation_generation"
        ),
        sa.CheckConstraint(
            "expected_session_row_version >= 1", name="ck_admin_runtime_operation_version"
        ),
        sa.CheckConstraint("status IN ('SUBMITTED')", name="ck_admin_runtime_operation_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id", "idempotency_key", name="uq_admin_runtime_operation_actor_key"
        ),
    )
    op.create_index(
        "ix_admin_runtime_operations_actor_user_id",
        "admin_runtime_operations",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_admin_runtime_operations_flow_run_id",
        "admin_runtime_operations",
        ["flow_run_id"],
    )
    op.create_index(
        "ix_admin_runtime_operations_runtime_session_id",
        "admin_runtime_operations",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_admin_runtime_operations_request_id",
        "admin_runtime_operations",
        ["request_id"],
    )
    op.create_index(
        "ix_admin_runtime_operations_created_at",
        "admin_runtime_operations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("admin_runtime_operations")
