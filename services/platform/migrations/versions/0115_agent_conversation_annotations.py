"""add user collaboration annotations for Agent conversations.

Revision ID: 0115_agent_annotations
Revises: 0114_runtime_sandbox_fk
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0115_agent_annotations"
down_revision = "0114_runtime_sandbox_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("agent_conversation_annotations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("anchor_kind", sa.String(length=30), nullable=False),
        sa.Column("anchor_json", sa.JSON(), nullable=False), sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="OPEN"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("anchor_kind IN ('CONVERSATION_TEXT', 'WORKSPACE_FILE_RANGE')", name="ck_agent_conversation_annotation_anchor_kind"),
        sa.CheckConstraint("state IN ('OPEN', 'RESOLVED')", name="ck_agent_conversation_annotation_state"))
    op.create_index("ix_agent_conversation_annotations_binding_id", "agent_conversation_annotations", ["binding_id"])
    op.create_index("ix_agent_conversation_annotations_state", "agent_conversation_annotations", ["state"])


def downgrade() -> None:
    op.drop_index("ix_agent_conversation_annotations_state", table_name="agent_conversation_annotations")
    op.drop_index("ix_agent_conversation_annotations_binding_id", table_name="agent_conversation_annotations")
    op.drop_table("agent_conversation_annotations")
