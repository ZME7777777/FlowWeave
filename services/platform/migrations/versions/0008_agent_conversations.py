"""agent conversations and append-only messages"""

from alembic import op
from sqlalchemy import text

from flowweave.modules.conversations.infrastructure import (
    models as conversation_models,  # noqa: F401
)
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0008_agent_conversations"
down_revision = "0007_run_event_notify"
branch_labels = None
depends_on = None
TABLES = ["agent_conversations", "agent_messages", "message_artifact_refs"]


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)
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
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
