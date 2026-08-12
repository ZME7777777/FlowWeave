"""add parent-owned subagent conversations

Revision ID: 0020_agent_subagents
Revises: 0019_hard_delete_legacy_flows
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_agent_subagents"
down_revision = "0019_hard_delete_legacy_flows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 0008 creates this table from live ORM metadata. Fresh installs
    # therefore already contain fields introduced after 0008, while existing
    # installations do not. Inspect before each operation so both paths remain
    # valid until 0008 can be replaced by a fully static table declaration.
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("agent_conversations")}
    if "parent_conversation_id" not in columns:
        op.add_column(
            "agent_conversations",
            sa.Column("parent_conversation_id", sa.String(length=36), nullable=True),
        )
    if "delegation_batch_key" not in columns:
        op.add_column(
            "agent_conversations",
            sa.Column("delegation_batch_key", sa.String(length=100), nullable=True),
        )
    if "delegation_instruction" not in columns:
        op.add_column(
            "agent_conversations",
            sa.Column("delegation_instruction", sa.Text(), nullable=True),
        )

    foreign_keys = sa.inspect(bind).get_foreign_keys("agent_conversations")
    has_parent_fk = any(
        item.get("constrained_columns") == ["parent_conversation_id"] for item in foreign_keys
    )
    if not has_parent_fk:
        op.create_foreign_key(
            "fk_agent_conversation_parent",
            "agent_conversations",
            "agent_conversations",
            ["parent_conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_conversations")}
    if "ix_agent_conversations_parent_conversation_id" not in indexes:
        op.create_index(
            "ix_agent_conversations_parent_conversation_id",
            "agent_conversations",
            ["parent_conversation_id"],
        )
    if "ix_agent_conversations_delegation_batch_key" not in indexes:
        op.create_index(
            "ix_agent_conversations_delegation_batch_key",
            "agent_conversations",
            ["delegation_batch_key"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("agent_conversations")}
    for name in (
        "ix_agent_conversations_delegation_batch_key",
        "ix_agent_conversations_parent_conversation_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="agent_conversations")

    foreign_keys = inspector.get_foreign_keys("agent_conversations")
    parent_fk = next(
        (
            item.get("name")
            for item in foreign_keys
            if item.get("constrained_columns") == ["parent_conversation_id"]
        ),
        None,
    )
    if parent_fk:
        op.drop_constraint(parent_fk, "agent_conversations", type_="foreignkey")

    columns = {item["name"] for item in inspector.get_columns("agent_conversations")}
    for name in (
        "delegation_instruction",
        "delegation_batch_key",
        "parent_conversation_id",
    ):
        if name in columns:
            op.drop_column("agent_conversations", name)
