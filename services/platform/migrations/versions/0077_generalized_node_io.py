"""Remove the retired Lark-root flow constraint.

Revision ID: 0077_generalized_node_io
Revises: 0076_remove_tool_policy
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0077_generalized_node_io"
down_revision = "0076_remove_tool_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("flow_definitions", "lark_root_folder_url")


def downgrade() -> None:
    op.add_column(
        "flow_definitions",
        sa.Column("lark_root_folder_url", sa.Text(), nullable=True),
    )
