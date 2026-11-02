"""Align existing archived directories and their durable conversations."""

import sqlalchemy as sa
from alembic import op

revision = "0184_cascade_directory_archives"
down_revision = "0183_user_market_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('flowweave.bypass', true)"))
    connection.execute(sa.text("SELECT set_config('flowweave.bypass', 'on', true)"))
    connection.execute(
        sa.text("""
        UPDATE agent_conversation_bindings AS binding
        SET archived_at = directory.archived_at,
            updated_at = GREATEST(binding.updated_at, directory.archived_at)
        FROM agent_work_directory_versions AS version
        JOIN agent_work_directories AS directory ON directory.id = version.work_directory_id
        WHERE binding.work_directory_version_id = version.id
          AND binding.workspace_id = directory.workspace_id
          AND directory.archived_at IS NOT NULL
          AND binding.archived_at IS NULL
          AND binding.lifecycle = 'ACTIVE'
          AND NOT EXISTS (
              SELECT 1 FROM agent_sidebar_conversations AS sidebar
              WHERE sidebar.sidebar_binding_id = binding.id
          )
    """)
    )
    connection.execute(
        sa.text("SELECT set_config('flowweave.bypass', :value, true)"), {"value": previous or ""}
    )


def downgrade() -> None:
    # Archive timestamps are user state. Reverting code must not unarchive data.
    pass
