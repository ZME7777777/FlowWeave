"""agent conversations and append-only messages"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0008_agent_conversations"
down_revision = "0007_run_event_notify"
branch_labels = None
depends_on = None


def _create_agent_conversations() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("runtime_adapter", sa.String(30), nullable=True),
        sa.Column("runtime_job_id", sa.String(100), nullable=True),
        sa.Column("runtime_conversation_id", sa.String(100), nullable=True, unique=True),
        sa.Column("runtime_cursor", sa.String(200), nullable=True),
        sa.Column("runtime_sandbox_id", sa.String(36), nullable=True),
        sa.Column("fork_kind", sa.String(20), nullable=True),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_runtime_conversation_id", sa.String(100), nullable=True),
        sa.Column("source_runtime_event_id", sa.String(200), nullable=True),
        sa.Column("runtime_branch_metadata_json", sa.JSON(), nullable=False),
        sa.Column("metrics_reset", sa.Boolean(), nullable=True),
        sa.Column("context_baseline_json", sa.JSON(), nullable=False),
        sa.Column("next_sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("created_by_type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "next_sequence_no > 0",
            name="ck_conversation_next_sequence_positive",
        ),
    )
    op.create_index(
        "uq_agent_conversation_number",
        "agent_conversations",
        ["attempt_id", "conversation_no"],
        unique=True,
    )
    op.create_index(
        "uq_agent_conversation_auto",
        "agent_conversations",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'AUTO'"),
    )
    op.create_index(
        "ix_agent_conversations_attempt_id",
        "agent_conversations",
        ["attempt_id"],
    )
    op.create_index(
        "ix_agent_conversations_runtime_sandbox_id",
        "agent_conversations",
        ["runtime_sandbox_id"],
    )
    op.create_index(
        "ix_agent_conversations_source_conversation_id",
        "agent_conversations",
        ["source_conversation_id"],
    )


def _create_agent_messages() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("transport_role", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(20), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("delivery_state", sa.String(20), nullable=False),
        sa.Column("delivery_mode", sa.String(30), nullable=True),
        sa.Column("client_message_id", sa.String(100), nullable=True),
        sa.Column("runtime_event_id", sa.String(200), nullable=True),
        sa.Column("runtime_cursor", sa.String(200), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_conversation_id",
        "agent_messages",
        ["conversation_id"],
    )
    op.create_index(
        "uq_agent_message_sequence",
        "agent_messages",
        ["conversation_id", "sequence_no"],
        unique=True,
    )
    op.create_index(
        "uq_agent_message_client_id",
        "agent_messages",
        ["conversation_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_agent_message_runtime_event",
        "agent_messages",
        ["conversation_id", "runtime_event_id"],
        unique=True,
        postgresql_where=sa.text("runtime_event_id IS NOT NULL"),
    )
    op.create_index(
        "ix_agent_message_delivery",
        "agent_messages",
        ["delivery_state", "created_at"],
    )


def _create_message_artifact_refs() -> None:
    op.create_table(
        "message_artifact_refs",
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("agent_messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "artifact_version_id",
            sa.String(36),
            sa.ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("relation_type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    bind = op.get_bind()
    _create_agent_conversations()
    _create_agent_messages()
    _create_message_artifact_refs()
    bind.execute(
        text("""
        INSERT INTO agent_conversations (
            id, attempt_id, conversation_no, kind, title, state, state_version,
            runtime_job_id, runtime_conversation_id, runtime_cursor,
            context_baseline_json, next_sequence_no, created_by_type, created_at, updated_at
        )
        SELECT gen_random_uuid()::text, id, 1, 'AUTO',
               '自动执行 · Attempt ' || attempt_no,
               CASE
                   WHEN state IN ('ACCEPTED','REJECTED','CANCELLED') THEN 'READ_ONLY'
                   ELSE 'IDLE'
               END,
               1, runtime_job_id, conversation_id, runtime_cursor,
               jsonb_build_object('snapshot_id', snapshot_id, 'legacy', true), 1, 'PROGRAM',
               created_at, updated_at
        FROM node_attempts
        WHERE conversation_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    )


def downgrade() -> None:
    op.drop_table("message_artifact_refs")
    op.drop_table("agent_messages")
    op.drop_table("agent_conversations")
