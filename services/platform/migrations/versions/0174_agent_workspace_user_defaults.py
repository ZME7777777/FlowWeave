"""Isolate workspace session credential defaults by user.

The nullable shared column introduced by 0173 is deliberately left untouched:
unattributed historical values must never be assigned to an arbitrary user.
"""

import sqlalchemy as sa
from alembic import op

revision = "0174_agent_workspace_user_defaults"
down_revision = "0173_agent_workspace_session_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "agent_workspace_session_configurations"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("credential_ids_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id", "workspace_id", name="uq_agent_workspace_session_owner"
        ),
    )
    for column in ("owner_user_id", "workspace_id"):
        op.create_index(f"ix_{table}_{column}", table, [column])
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    policy = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(f"CREATE POLICY pl_tenant_{table} ON {table} USING ({policy}) WITH CHECK ({policy})")


def downgrade() -> None:
    table = "agent_workspace_session_configurations"
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
        raise RuntimeError("Cannot discard persisted user session defaults during downgrade")
    op.drop_table(table)
