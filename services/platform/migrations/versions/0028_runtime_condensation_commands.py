"""add durable native condensation commands

Revision ID: 0028_condensation_commands
Revises: 0027_runtime_condensations
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_condensation_commands"
down_revision = "0027_runtime_condensations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "runtime_condensation_commands" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "runtime_condensation_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime_conversation_id", sa.String(100), nullable=False),
        sa.Column("baseline_cursor", sa.String(200), nullable=True),
        sa.Column("request_event_id", sa.String(200), nullable=True),
        sa.Column("completion_event_id", sa.String(200), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("requested_by", sa.String(160), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_runtime_condensation_command_state",
        ),
        sa.CheckConstraint(
            "state_version > 0",
            name="ck_runtime_condensation_command_version_positive",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_runtime_condensation_command_idempotency"),
    )
    op.create_index(
        "ix_runtime_condensation_commands_attempt_id",
        "runtime_condensation_commands",
        ["attempt_id"],
    )
    op.create_index(
        "ix_runtime_condensation_commands_conversation_id",
        "runtime_condensation_commands",
        ["conversation_id"],
    )
    op.create_index(
        "uq_runtime_condensation_command_active",
        "runtime_condensation_commands",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "runtime_condensation_commands" in sa.inspect(bind).get_table_names():
        op.drop_table("runtime_condensation_commands")
