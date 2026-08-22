"""add minimal FlowRun OpenHands Conversation locators

Revision ID: 0055_conversation_bindings
Revises: 0054_runtime_sessions

Historical AgentConversation rows are intentionally not backfilled. Their
container-scoped identities cannot prove a binding to the new stable Runtime
Session and must fail closed until explicitly rerun on the new path.
"""

import sqlalchemy as sa
from alembic import op

revision = "0055_conversation_bindings"
down_revision = "0054_runtime_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_flow_run_runtime_session_owner",
        "flow_run_runtimes",
        ["id", "flow_run_id"],
    )
    op.create_table(
        "flow_run_conversation_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=36), nullable=False),
        sa.Column("openhands_conversation_id", sa.String(length=100), nullable=False),
        sa.Column("display_label", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["flow_run_id"],
            ["flow_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_session_id", "flow_run_id"],
            ["flow_run_runtimes.id", "flow_run_runtimes.flow_run_id"],
            name="fk_flow_run_conversation_runtime_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_session_id",
            "openhands_conversation_id",
            name="uq_flow_run_conversation_runtime_identity",
        ),
    )
    op.create_index(
        "ix_flow_run_conversation_bindings_flow_run_id",
        "flow_run_conversation_bindings",
        ["flow_run_id"],
    )
    op.create_index(
        "ix_flow_run_conversation_bindings_runtime_session_id",
        "flow_run_conversation_bindings",
        ["runtime_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_flow_run_conversation_bindings_runtime_session_id",
        table_name="flow_run_conversation_bindings",
    )
    op.drop_index(
        "ix_flow_run_conversation_bindings_flow_run_id",
        table_name="flow_run_conversation_bindings",
    )
    op.drop_table("flow_run_conversation_bindings")
    op.drop_constraint(
        "uq_flow_run_runtime_session_owner",
        "flow_run_runtimes",
        type_="unique",
    )
