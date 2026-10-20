"""add self-contained Agent schedule template settings.

Revision ID: 0154_agent_schedule_templates
Revises: 0153_agent_conversation_schedules
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0154_agent_schedule_templates"
down_revision = "0153_agent_conversation_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("work_directory_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column(
            "workspace_mode", sa.String(length=20), nullable=False, server_default="EXISTING"
        ),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("conversation_mode", sa.String(length=20), nullable=False, server_default="NEW"),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("model_provider_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("model_name", sa.String(length=240), nullable=True),
    )
    op.add_column(
        "agent_conversation_schedules",
        sa.Column("reasoning_effort", sa.String(length=30), nullable=True),
    )
    op.alter_column("agent_conversation_schedules", "binding_id", nullable=True)
    op.create_index(
        "ix_agent_conversation_schedules_work_directory_id",
        "agent_conversation_schedules",
        ["work_directory_id"],
    )
    op.create_index(
        "ix_agent_conversation_schedules_model_provider_id",
        "agent_conversation_schedules",
        ["model_provider_id"],
    )
    op.create_check_constraint(
        "ck_agent_schedule_workspace_mode",
        "agent_conversation_schedules",
        "workspace_mode IN ('AUTOMATION', 'EXISTING')",
    )
    op.create_check_constraint(
        "ck_agent_schedule_conversation_mode",
        "agent_conversation_schedules",
        "conversation_mode IN ('NEW', 'CONTINUE')",
    )
    # Legacy tasks depended on an arbitrary source conversation. Pause them
    # until their owner explicitly chooses a workspace and a frozen model.
    op.execute(
        sa.text("UPDATE agent_conversation_schedules SET status = 'PAUSED', next_run_at = NULL")
    )
    op.alter_column("agent_conversation_schedules", "workspace_mode", server_default=None)
    op.alter_column("agent_conversation_schedules", "conversation_mode", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_agent_schedule_conversation_mode", "agent_conversation_schedules")
    op.drop_constraint("ck_agent_schedule_workspace_mode", "agent_conversation_schedules")
    op.drop_index(
        "ix_agent_conversation_schedules_model_provider_id", "agent_conversation_schedules"
    )
    op.drop_index(
        "ix_agent_conversation_schedules_work_directory_id", "agent_conversation_schedules"
    )
    op.alter_column("agent_conversation_schedules", "binding_id", nullable=False)
    op.drop_column("agent_conversation_schedules", "reasoning_effort")
    op.drop_column("agent_conversation_schedules", "model_name")
    op.drop_column("agent_conversation_schedules", "model_provider_id")
    op.drop_column("agent_conversation_schedules", "conversation_mode")
    op.drop_column("agent_conversation_schedules", "workspace_mode")
    op.drop_column("agent_conversation_schedules", "work_directory_id")
