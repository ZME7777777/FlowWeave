"""Project the formal native event-log count for conversation management."""

import sqlalchemy as sa
from alembic import op

revision = "0194_conversation_event_count"
down_revision = "0193_case_copy_stepwise_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("stored_event_count", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_agent_conversation_event_count",
        "agent_conversation_bindings",
        "stored_event_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_conversation_event_count", "agent_conversation_bindings", type_="check"
    )
    op.drop_column("agent_conversation_bindings", "stored_event_count")
