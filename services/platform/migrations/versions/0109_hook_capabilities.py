"""Allow creation-time Agent Definition and Hook references.

Revision ID: 0109_hook_capabilities
Revises: 0108_model_provider_api_protocol
Create Date: 2026-09-10
"""

from alembic import op

revision = "0109_hook_capabilities"
down_revision = "0108_model_provider_api_protocol"
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
        "capability_type IN ('SKILL', 'MCP', 'PLUGIN', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK')",
    )


def downgrade() -> None:
    # These types are creation-time frozen references. Older schemas cannot
    # represent them, so remove their references before restoring the check.
    op.execute(
        "DELETE FROM agent_conversation_capabilities "
        "WHERE capability_type IN ('AGENT_DEFINITION', 'HOOK')"
    )
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
