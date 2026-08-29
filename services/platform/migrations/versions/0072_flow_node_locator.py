"""Cut FlowRun conversations over to the shared Agent-session locator.

Revision ID: 0072_flow_node_locator
Revises: 0071_shared_agent_session_hosts
Create Date: 2026-08-29

The old FlowRun locator and its confirmation projections cannot prove a node
scope or frozen workspace directory.  The refactor explicitly does not retain
that history, so this migration discards those unverifiable rows rather than
inventing lineage.  All new FlowRun sessions use agent_conversation_bindings.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072_flow_node_locator"
down_revision = "0071_shared_agent_session_hosts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_conversation_bindings",
        "openhands_conversation_id",
        existing_type=sa.String(length=36),
        type_=sa.String(length=100),
    )
    # A confirmation record is useful only with a durable, authorized locator.
    # Old rows point at the removed locator table and cannot be reconstructed.
    op.execute(sa.text("DELETE FROM runtime_confirmation_approvals"))
    op.drop_index(
        "uq_runtime_confirmation_approval_active",
        table_name="runtime_confirmation_approvals",
    )
    old_binding_fk = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'runtime_confirmation_approvals'::regclass "
                "AND contype = 'f' "
                "AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
                "WHERE attrelid = 'runtime_confirmation_approvals'::regclass "
                "AND attname = 'flow_run_conversation_binding_id')]"
            )
        )
        .scalar_one()
    )
    op.drop_constraint(
        old_binding_fk,
        "runtime_confirmation_approvals",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_runtime_confirmation_shared_binding",
        "runtime_confirmation_approvals",
        "agent_conversation_bindings",
        ["flow_run_conversation_binding_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_runtime_confirmation_approval_active",
        "runtime_confirmation_approvals",
        ["flow_run_conversation_binding_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'DECIDING')"),
    )
    op.drop_index(
        "ix_flow_run_conversation_bindings_runtime_session_id",
        table_name="flow_run_conversation_bindings",
    )
    op.drop_index(
        "ix_flow_run_conversation_bindings_flow_run_id",
        table_name="flow_run_conversation_bindings",
    )
    op.drop_table("flow_run_conversation_bindings")


def downgrade() -> None:
    # The upgrade deliberately discarded unverifiable history.  Recreate the
    # old empty schema so Alembic downgrade remains structurally reversible.
    op.execute(sa.text("DELETE FROM runtime_confirmation_approvals"))
    op.drop_index(
        "uq_runtime_confirmation_approval_active",
        table_name="runtime_confirmation_approvals",
    )
    op.drop_constraint(
        "fk_runtime_confirmation_shared_binding",
        "runtime_confirmation_approvals",
        type_="foreignkey",
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
        sa.ForeignKeyConstraint(["flow_run_id"], ["flow_runs.id"], ondelete="CASCADE"),
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
    op.create_foreign_key(
        "fk_runtime_confirmation_flow_binding",
        "runtime_confirmation_approvals",
        "flow_run_conversation_bindings",
        ["flow_run_conversation_binding_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_runtime_confirmation_approval_active",
        "runtime_confirmation_approvals",
        ["flow_run_conversation_binding_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'DECIDING')"),
    )
    op.alter_column(
        "agent_conversation_bindings",
        "openhands_conversation_id",
        existing_type=sa.String(length=100),
        type_=sa.String(length=36),
    )
