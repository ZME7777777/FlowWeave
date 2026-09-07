"""Remove the deprecated workspace-level model fallback.

Revision ID: 0099_remove_workspace_default_model
Revises: 0098_schedule_templates_cron
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0099_remove_workspace_default_model"
down_revision = "0098_schedule_templates_cron"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_agent_workspace_preferences_default_model_provider_id",
        table_name="agent_workspace_preferences",
    )
    op.drop_index(
        "ix_agent_workspace_preferences_workspace_id",
        table_name="agent_workspace_preferences",
    )
    op.drop_index(
        "ix_agent_workspace_preferences_owner_user_id",
        table_name="agent_workspace_preferences",
    )
    op.drop_table("agent_workspace_preferences")
    op.drop_index("ix_agent_workspaces_default_model_provider_id", table_name="agent_workspaces")
    # Some pre-0099 deployments lost this constraint during an earlier
    # partially-applied migration. PostgreSQL's IF EXISTS keeps this cleanup
    # migration idempotent while the column/index drops below remain strict.
    op.execute(
        sa.text(
            "ALTER TABLE agent_workspaces "
            "DROP CONSTRAINT IF EXISTS fk_agent_workspaces_default_model_provider"
        )
    )
    op.drop_column("agent_workspaces", "default_model_provider_id")


def downgrade() -> None:
    op.add_column(
        "agent_workspaces",
        sa.Column("default_model_provider_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_workspaces_default_model_provider",
        "agent_workspaces",
        "model_providers",
        ["default_model_provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_workspaces_default_model_provider_id",
        "agent_workspaces",
        ["default_model_provider_id"],
    )
    op.create_table(
        "agent_workspace_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("default_model_provider_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "workspace_id", name="uq_agent_workspace_preference_owner"
        ),
    )
    op.create_index(
        "ix_agent_workspace_preferences_workspace_id",
        "agent_workspace_preferences",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_workspace_preferences_default_model_provider_id",
        "agent_workspace_preferences",
        ["default_model_provider_id"],
    )
    op.create_index(
        "ix_agent_workspace_preferences_owner_user_id",
        "agent_workspace_preferences",
        ["owner_user_id"],
    )
