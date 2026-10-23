"""Freeze governed history capability provenance and native registration acknowledgement."""

import sqlalchemy as sa
from alembic import op

revision = "0161_agent_history_capability"
down_revision = "0160_agent_history_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_history_sessions", sa.Column("capability_version_id", sa.String(36)))
    op.add_column("agent_history_sessions", sa.Column("capability_digest", sa.String(64)))
    op.add_column(
        "agent_history_sessions",
        sa.Column(
            "runtime_manifest_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.add_column("agent_history_sessions", sa.Column("registered_at", sa.DateTime(timezone=True)))
    op.alter_column("agent_history_sessions", "runtime_manifest_json", server_default=None)


def downgrade() -> None:
    for name in (
        "registered_at",
        "runtime_manifest_json",
        "capability_digest",
        "capability_version_id",
    ):
        op.drop_column("agent_history_sessions", name)
