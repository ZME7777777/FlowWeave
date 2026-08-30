"""Freeze governed capabilities on direct Agent conversations.

Revision ID: 0070_agent_caps
Revises: 0069_agent_message_attachments
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0070_agent_caps"
down_revision = "0069_agent_message_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_workspace_capabilities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("capability_version_id", sa.String(36), nullable=False),
        sa.Column("capability_type", sa.String(20), nullable=False),
        sa.Column("capability_key", sa.String(160), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
            name="ck_agent_workspace_capability_type",
        ),
        sa.CheckConstraint("position >= 0", name="ck_agent_workspace_capability_position"),
        sa.ForeignKeyConstraint(["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["capability_version_id"], ["capability_versions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id", "capability_version_id", name="uq_agent_workspace_capability"
        ),
        sa.UniqueConstraint(
            "workspace_id", "position", name="uq_agent_workspace_capability_position"
        ),
    )
    op.create_index(
        "ix_agent_workspace_capabilities_workspace_id",
        "agent_workspace_capabilities",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_workspace_capabilities_capability_version_id",
        "agent_workspace_capabilities",
        ["capability_version_id"],
    )
    op.create_table(
        "agent_conversation_capabilities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("capability_version_id", sa.String(36), nullable=False),
        sa.Column("capability_type", sa.String(20), nullable=False),
        sa.Column("capability_key", sa.String(160), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
            name="ck_agent_conversation_capability_type",
        ),
        sa.CheckConstraint("position >= 0", name="ck_agent_conversation_capability_position"),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["agent_conversation_bindings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["capability_version_id"], ["capability_versions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "binding_id", "capability_version_id", name="uq_agent_conversation_capability"
        ),
        sa.UniqueConstraint(
            "binding_id", "position", name="uq_agent_conversation_capability_position"
        ),
    )
    op.create_index(
        "ix_agent_conversation_capabilities_binding_id",
        "agent_conversation_capabilities",
        ["binding_id"],
    )
    op.create_index(
        "ix_agent_conversation_capabilities_capability_version_id",
        "agent_conversation_capabilities",
        ["capability_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_capabilities_capability_version_id",
        table_name="agent_conversation_capabilities",
    )
    op.drop_index(
        "ix_agent_conversation_capabilities_binding_id",
        table_name="agent_conversation_capabilities",
    )
    op.drop_table("agent_conversation_capabilities")
    op.drop_index(
        "ix_agent_workspace_capabilities_capability_version_id",
        table_name="agent_workspace_capabilities",
    )
    op.drop_index(
        "ix_agent_workspace_capabilities_workspace_id",
        table_name="agent_workspace_capabilities",
    )
    op.drop_table("agent_workspace_capabilities")
