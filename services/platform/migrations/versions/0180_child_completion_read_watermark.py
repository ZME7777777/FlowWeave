"""Schedule child completion reads even when a fast running sample was missed."""

import sqlalchemy as sa
from alembic import op

revision = "0180_child_completion_read_watermark"
down_revision = "0179_agent_child_creation_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("child_completion_checked_activity_at", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_conversation_bindings", "child_completion_checked_activity_at")
