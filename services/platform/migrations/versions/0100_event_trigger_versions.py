"""persist governed runtime event trigger versions and actions

Revision ID: 0100_event_trigger_versions
Revises: 0099_remove_ws_default_model
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100_event_trigger_versions"
down_revision = "0099_remove_ws_default_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_trigger_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trigger_key", sa.String(length=120), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("event_filter_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("version_no >= 1", name="ck_event_trigger_version_positive"),
        sa.CheckConstraint("length(trim(trigger_key)) > 0", name="ck_event_trigger_key_nonblank"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "trigger_key",
            "version_no",
            name="uq_event_trigger_version_owner_key_no",
        ),
    )
    op.create_index(
        "ix_event_trigger_versions_trigger_key", "event_trigger_versions", ["trigger_key"]
    )
    op.create_index(
        "ix_event_trigger_versions_owner_user_id", "event_trigger_versions", ["owner_user_id"]
    )

    op.create_table(
        "event_trigger_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trigger_version_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=60), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_event_trigger_action_position_nonnegative"),
        sa.CheckConstraint(
            "action_type IN ('RESUME_CONVERSATION', 'WEBHOOK', 'NOTIFY', 'CREATE_TASK', "
            "'PAUSE_ATTEMPT', 'HANDOFF_HUMAN')",
            name="ck_event_trigger_action_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "trigger_version_id",
            "position",
            name="uq_event_trigger_action_owner_version_position",
        ),
    )
    op.create_index(
        "ix_event_trigger_actions_trigger_version_id",
        "event_trigger_actions",
        ["trigger_version_id"],
    )
    op.create_index(
        "ix_event_trigger_actions_owner_user_id", "event_trigger_actions", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_event_trigger_actions_owner_user_id", table_name="event_trigger_actions")
    op.drop_index("ix_event_trigger_actions_trigger_version_id", table_name="event_trigger_actions")
    op.drop_table("event_trigger_actions")
    op.drop_index("ix_event_trigger_versions_owner_user_id", table_name="event_trigger_versions")
    op.drop_index("ix_event_trigger_versions_trigger_key", table_name="event_trigger_versions")
    op.drop_table("event_trigger_versions")
