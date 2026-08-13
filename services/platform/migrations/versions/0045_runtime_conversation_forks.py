"""project native OpenHands conversation forks

Revision ID: 0045_runtime_conversation_forks
Revises: 0044_memory_source_retention
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_runtime_conversation_forks"
down_revision = "0044_memory_source_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_conversations")}
    additions = (
        sa.Column("fork_kind", sa.String(20), nullable=True),
        sa.Column("source_conversation_id", sa.String(36), nullable=True),
        sa.Column("source_runtime_conversation_id", sa.String(100), nullable=True),
        sa.Column("source_runtime_event_id", sa.String(200), nullable=True),
        sa.Column(
            "runtime_branch_metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("metrics_reset", sa.Boolean(), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("agent_conversations", column)

    inspector = sa.inspect(bind)
    checks = {item["name"] for item in inspector.get_check_constraints("agent_conversations")}
    if "ck_agent_conversation_fork_kind" not in checks:
        op.create_check_constraint(
            "ck_agent_conversation_fork_kind",
            "agent_conversations",
            "fork_kind IS NULL OR fork_kind IN ('RUNTIME', 'SEMANTIC')",
        )
    foreign_keys = inspector.get_foreign_keys("agent_conversations")
    if not any(
        item.get("constrained_columns") == ["source_conversation_id"] for item in foreign_keys
    ):
        op.create_foreign_key(
            "fk_agent_conversation_source",
            "agent_conversations",
            "agent_conversations",
            ["source_conversation_id"],
            ["id"],
            ondelete="SET NULL",
        )
    indexes = {item["name"] for item in inspector.get_indexes("agent_conversations")}
    if "ix_agent_conversations_source_conversation_id" not in indexes:
        op.create_index(
            "ix_agent_conversations_source_conversation_id",
            "agent_conversations",
            ["source_conversation_id"],
        )

    if "runtime_conversation_forks" in inspector.get_table_names():
        return
    op.create_table(
        "runtime_conversation_forks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("runtime_adapter", sa.String(30), nullable=False),
        sa.Column("runtime_job_id", sa.String(100), nullable=False),
        sa.Column("runtime_sandbox_id", sa.String(36), nullable=True),
        sa.Column("source_runtime_conversation_id", sa.String(100), nullable=False),
        sa.Column("target_runtime_conversation_id", sa.String(100), nullable=False, unique=True),
        sa.Column("requested_from_event_id", sa.String(200), nullable=True),
        sa.Column("source_head_event_id", sa.String(200), nullable=False),
        sa.Column("resolved_source_event_id", sa.String(200), nullable=True),
        sa.Column("fork_leaf_event_id", sa.String(200), nullable=True),
        sa.Column("reset_metrics", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("source_state_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'FAILED')",
            name="ck_runtime_conversation_fork_state",
        ),
        sa.CheckConstraint(
            "state_version > 0 AND source_state_version > 0",
            name="ck_runtime_conversation_fork_versions",
        ),
    )
    for column in (
        "attempt_id",
        "source_conversation_id",
        "target_conversation_id",
        "state",
    ):
        op.create_index(
            f"ix_runtime_conversation_forks_{column}",
            "runtime_conversation_forks",
            [column],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "runtime_conversation_forks" in inspector.get_table_names():
        op.drop_table("runtime_conversation_forks")

    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("agent_conversations")}
    if "ix_agent_conversations_source_conversation_id" in indexes:
        op.drop_index(
            "ix_agent_conversations_source_conversation_id",
            table_name="agent_conversations",
        )
    foreign_keys = {item.get("name") for item in inspector.get_foreign_keys("agent_conversations")}
    if "fk_agent_conversation_source" in foreign_keys:
        op.drop_constraint(
            "fk_agent_conversation_source",
            "agent_conversations",
            type_="foreignkey",
        )
    checks = {item["name"] for item in inspector.get_check_constraints("agent_conversations")}
    if "ck_agent_conversation_fork_kind" in checks:
        op.drop_constraint(
            "ck_agent_conversation_fork_kind",
            "agent_conversations",
            type_="check",
        )
    columns = {column["name"] for column in inspector.get_columns("agent_conversations")}
    for column in (
        "metrics_reset",
        "runtime_branch_metadata_json",
        "source_runtime_event_id",
        "source_runtime_conversation_id",
        "source_conversation_id",
        "fork_kind",
    ):
        if column in columns:
            op.drop_column("agent_conversations", column)
