"""Freeze explicit model fallback policy on Agent conversation bindings.

Revision ID: 0112_agent_fallback_models
Revises: 0111_environment_runtime_capabilities
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0112_agent_fallback_models"
down_revision = "0111_environment_runtime_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column(
            "fallback_models_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.alter_column("agent_conversation_bindings", "fallback_models_json", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "fallback_models_json")
