"""persist runtime provenance and cancellation outcome"""

from alembic import op
from sqlalchemy import text

revision = "0009_runtime_cancellation"
down_revision = "0008_agent_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS runtime_adapter VARCHAR(30)")
    op.execute(
        "ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS runtime_adapter VARCHAR(30)"
    )
    bind = op.get_bind()
    bind.execute(
        text("""
        UPDATE node_attempts
        SET runtime_adapter = CASE
            WHEN runtime_job_id LIKE 'mock-%' OR conversation_id LIKE 'mock-%' THEN 'mock'
            WHEN runtime_job_id IS NOT NULL OR conversation_id IS NOT NULL THEN 'openhands'
            ELSE NULL
        END
        WHERE runtime_adapter IS NULL
        """)
    )
    bind.execute(
        text("""
        UPDATE agent_conversations
        SET runtime_adapter = CASE
            WHEN runtime_job_id LIKE 'mock-%' OR runtime_conversation_id LIKE 'mock-%' THEN 'mock'
            WHEN runtime_job_id IS NOT NULL OR runtime_conversation_id IS NOT NULL THEN 'openhands'
            ELSE NULL
        END
        WHERE runtime_adapter IS NULL
        """)
    )
    bind.execute(
        text("""
        UPDATE node_attempts
        SET runtime_phase = 'CANCELLED', error_code = NULL, error_detail = NULL
        WHERE state = 'CANCELLED'
          AND runtime_phase = 'CANCELLING'
          AND runtime_adapter = 'mock'
        """)
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS runtime_adapter")
    op.execute("ALTER TABLE node_attempts DROP COLUMN IF EXISTS runtime_adapter")
