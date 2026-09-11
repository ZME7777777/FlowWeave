"""Freeze explicit model fallback policy on Agent conversation bindings.

Revision ID: 0112_agent_fallback
Revises: 0111_env_runtime_caps
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0112_agent_fallback"
down_revision = "0111_env_runtime_caps"
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
