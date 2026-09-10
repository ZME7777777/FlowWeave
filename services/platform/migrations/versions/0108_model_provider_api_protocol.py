"""add configured OpenAI-compatible provider protocol

Revision ID: 0108_model_provider_api_protocol
Revises: 0107_usage_reconcile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0108_model_provider_api_protocol"
down_revision = "0107_usage_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_providers",
        sa.Column(
            "api_protocol",
            sa.String(length=30),
            nullable=False,
            server_default="CHAT_COMPLETIONS",
        ),
    )
    op.execute(
        "UPDATE model_providers SET api_protocol = 'RESPONSES' WHERE auth_type = 'CODEX_OAUTH'"
    )


def downgrade() -> None:
    op.drop_column("model_providers", "api_protocol")
