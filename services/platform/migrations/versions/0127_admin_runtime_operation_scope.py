"""Generalize administrator Runtime operations to Agent Workspaces.

Revision ID: 0127_admin_runtime_scope
Revises: 0126_unread_origin
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0127_admin_runtime_scope"
down_revision = "0126_unread_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_runtime_operations",
        sa.Column("runtime_kind", sa.String(length=30), nullable=False, server_default="FLOW_RUN"),
    )
    op.add_column(
        "admin_runtime_operations",
        sa.Column("owner_id", sa.String(length=36), nullable=True),
    )
    op.execute("UPDATE admin_runtime_operations SET owner_id = flow_run_id")
    op.alter_column("admin_runtime_operations", "owner_id", nullable=False)
    op.alter_column("admin_runtime_operations", "flow_run_id", nullable=True)
    op.create_index(
        "ix_admin_runtime_operations_runtime_kind",
        "admin_runtime_operations",
        ["runtime_kind"],
    )
    op.create_index(
        "ix_admin_runtime_operations_owner_id", "admin_runtime_operations", ["owner_id"]
    )
    op.create_check_constraint(
        "ck_admin_runtime_operation_runtime_kind",
        "admin_runtime_operations",
        "runtime_kind IN ('FLOW_RUN', 'AGENT_WORKSPACE')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_admin_runtime_operation_runtime_kind",
        "admin_runtime_operations",
        type_="check",
    )
    op.drop_index("ix_admin_runtime_operations_owner_id", table_name="admin_runtime_operations")
    op.drop_index("ix_admin_runtime_operations_runtime_kind", table_name="admin_runtime_operations")
    op.execute("DELETE FROM admin_runtime_operations WHERE runtime_kind <> 'FLOW_RUN'")
    op.alter_column("admin_runtime_operations", "flow_run_id", nullable=False)
    op.drop_column("admin_runtime_operations", "owner_id")
    op.drop_column("admin_runtime_operations", "runtime_kind")
