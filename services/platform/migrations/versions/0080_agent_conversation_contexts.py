"""Permit creation-time Context references on Agent conversations.

Revision ID: 0080_agent_conversation_contexts
Revises: 0079_context_capabilities
Create Date: 2026-08-31
"""

from alembic import op

revision = "0080_agent_conversation_contexts"
down_revision = "0079_context_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_capability_type",
        "agent_conversation_capabilities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_capability_type",
        "agent_conversation_capabilities",
        "capability_type IN ('SKILL', 'MCP', 'PLUGIN', 'CONTEXT')",
    )


def downgrade() -> None:
    # A Context is only a creation-time system suffix. Older schemas cannot
    # represent it, so remove its frozen references before restoring the
    # pre-Context constraint. The OpenHands conversation itself is untouched.
    op.execute(
        "DELETE FROM agent_conversation_capabilities WHERE capability_type = 'CONTEXT'"
    )
    op.drop_constraint(
        "ck_agent_conversation_capability_type",
        "agent_conversation_capabilities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_capability_type",
        "agent_conversation_capabilities",
        "capability_type IN ('SKILL', 'MCP', 'PLUGIN')",
    )
