"""add standalone Agent conversation schedules.

Revision ID: 0153_agent_conversation_schedules
Revises: 0152_long_lived_user_sessions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0153_agent_conversation_schedules"
down_revision = "0152_long_lived_user_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_schedules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron_expression", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'PAUSED')", name="ck_agent_schedule_status"),
        sa.CheckConstraint("length(cron_expression) > 0", name="ck_agent_schedule_cron_present"),
        sa.CheckConstraint("row_version >= 1", name="ck_agent_schedule_row_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_conversation_schedules_owner_user_id", "agent_conversation_schedules", ["owner_user_id"])
    op.create_index("ix_agent_conversation_schedules_workspace_id", "agent_conversation_schedules", ["workspace_id"])
    op.create_index("ix_agent_conversation_schedules_binding_id", "agent_conversation_schedules", ["binding_id"])
    op.create_index("ix_agent_conversation_schedules_status", "agent_conversation_schedules", ["status"])
    op.create_index("ix_agent_conversation_schedules_next_run_at", "agent_conversation_schedules", ["next_run_at"])
    op.create_table(
        "agent_conversation_schedule_occurrences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_kind", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("message_cursor", sa.String(length=200), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("trigger_kind IN ('SCHEDULED', 'MANUAL')", name="ck_agent_schedule_occurrence_trigger"),
        sa.CheckConstraint("state IN ('PENDING', 'STARTED', 'FAILED')", name="ck_agent_schedule_occurrence_state"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_id", "scheduled_for", name="uq_agent_schedule_occurrence_slot"),
    )
    op.create_index("ix_agent_conversation_schedule_occurrences_owner_user_id", "agent_conversation_schedule_occurrences", ["owner_user_id"])
    op.create_index("ix_agent_conversation_schedule_occurrences_schedule_id", "agent_conversation_schedule_occurrences", ["schedule_id"])
    op.create_index("ix_agent_conversation_schedule_occurrences_binding_id", "agent_conversation_schedule_occurrences", ["binding_id"])
    op.create_index("ix_agent_conversation_schedule_occurrences_state", "agent_conversation_schedule_occurrences", ["state"])
    for table in ("agent_conversation_schedules", "agent_conversation_schedule_occurrences"):
        policy = f"pl_tenant_{table}"
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                f'CREATE POLICY "{policy}" ON "{table}" '
                "USING (current_setting('flowweave.bypass', true) = 'on' OR "
                "owner_user_id = current_setting('flowweave.user_id', true)) "
                "WITH CHECK (current_setting('flowweave.bypass', true) = 'on' OR "
                "owner_user_id = current_setting('flowweave.user_id', true))"
            )
        )


def downgrade() -> None:
    op.drop_table("agent_conversation_schedule_occurrences")
    op.drop_table("agent_conversation_schedules")
