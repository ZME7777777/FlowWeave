"""Audit administrator cleanup of expired background-task records.

Revision ID: 0138_admin_resource_cleanup_operations
Revises: 0137_conversation_message_order
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0138_admin_resource_cleanup_operations"
down_revision = "0137_conversation_message_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic writes this 38-character revision after upgrade() returns. Widen
    # its default VARCHAR(32) in the same transaction, including on fresh DBs.
    # Preserve larger/unbounded columns and the caller's lock timeout.
    op.execute(
        """
        DO $$
        DECLARE previous_lock_timeout text := current_setting('lock_timeout');
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_attribute
                WHERE attrelid = 'alembic_version'::regclass
                  AND attname = 'version_num'
                  AND atttypid = 'varchar'::regtype
                  AND atttypmod > 4 AND atttypmod < 132
            ) THEN
                PERFORM set_config('lock_timeout', '5s', true);
                ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128);
                PERFORM set_config('lock_timeout', previous_lock_timeout, true);
            END IF;
        END
        $$;
        """
    )
    op.create_table(
        "admin_resource_cleanup_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("actor_username", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action = 'CLEANUP_EXPIRED_TASKS'",
            name="ck_admin_resource_cleanup_operation_action",
        ),
        sa.CheckConstraint(
            "retention_days >= 1", name="ck_admin_resource_cleanup_operation_retention"
        ),
        sa.CheckConstraint(
            "batch_size >= 1 AND batch_size <= 500",
            name="ck_admin_resource_cleanup_operation_batch",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_admin_resource_cleanup_operation_actor_key",
        ),
    )
    op.create_index(
        "ix_admin_resource_cleanup_operations_actor_user_id",
        "admin_resource_cleanup_operations",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_admin_resource_cleanup_operations_request_id",
        "admin_resource_cleanup_operations",
        ["request_id"],
    )
    op.create_index(
        "ix_admin_resource_cleanup_operations_created_at",
        "admin_resource_cleanup_operations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("admin_resource_cleanup_operations")
