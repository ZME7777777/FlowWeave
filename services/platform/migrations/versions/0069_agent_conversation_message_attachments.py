"""Project Agent Conversation attachment cards by native message identity.

Revision ID: 0069_agent_message_attachments
Revises: 0068_agent_title_metadata
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0069_agent_message_attachments"
down_revision = "0068_agent_title_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_message_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("mime_type", sa.String(200), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["agent_conversation_bindings.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "binding_id", "event_id", "path", name="uq_agent_conversation_message_attachment"
        ),
    )
    op.create_index(
        "ix_agent_conversation_message_attachments_binding_id",
        "agent_conversation_message_attachments",
        ["binding_id"],
    )
    op.create_index(
        "ix_agent_conversation_message_attachments_event_id",
        "agent_conversation_message_attachments",
        ["event_id"],
    )
    op.alter_column("agent_conversation_message_attachments", "content", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversation_message_attachments_event_id",
        table_name="agent_conversation_message_attachments",
    )
    op.drop_index(
        "ix_agent_conversation_message_attachments_binding_id",
        table_name="agent_conversation_message_attachments",
    )
    op.drop_table("agent_conversation_message_attachments")
