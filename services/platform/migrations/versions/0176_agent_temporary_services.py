"""Track temporary listening services created in Agent workspaces."""

import sqlalchemy as sa
from alembic import op

revision = "0176_agent_temporary_services"
down_revision = "0175_merge_workspace_session_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "agent_temporary_services"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("binding_id", sa.String(36), nullable=True),
        sa.Column("sandbox_id", sa.String(36), nullable=False),
        sa.Column("runtime_generation", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("process_started_ticks", sa.String(40), nullable=False),
        sa.Column("process_group_id", sa.Integer(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("working_directory", sa.String(500), nullable=True),
        sa.Column("ports_json", sa.JSON(), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('REGISTERED', 'DISCOVERED')", name="ck_agent_temp_service_source"
        ),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'STOPPING', 'STOPPED', 'MISSING')",
            name="ck_agent_temp_service_state",
        ),
        sa.UniqueConstraint(
            "sandbox_id", "runtime_generation", "pid", "process_started_ticks",
            name="uq_agent_temp_service_process",
        ),
    )
    for column in (
        "owner_user_id",
        "workspace_id",
        "binding_id",
        "sandbox_id",
        "state",
        "expires_at",
    ):
        op.create_index(f"ix_{table}_{column}", table, [column])
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    policy = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(f"CREATE POLICY pl_tenant_{table} ON {table} USING ({policy}) WITH CHECK ({policy})")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE agent_temporary_services IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_temporary_services LIMIT 1")).first():
        raise RuntimeError("Cannot discard tracked temporary services during downgrade")
    op.drop_table("agent_temporary_services")
