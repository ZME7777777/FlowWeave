"""Persist Case basics, materials and conversation defaults.

Revision ID: 0172_case_configuration
Revises: 0171_remove_flow_run_revision_drafts
"""

import sqlalchemy as sa
from alembic import op

revision = "0172_case_configuration"
down_revision = "0171_remove_flow_run_revision_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "cases", sa.Column("materials_json", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "cases", sa.Column("session_config_json", sa.JSON(), nullable=False, server_default="{}")
    )
    op.add_column("cases", sa.Column("work_directory_id", sa.String(36), nullable=True))


def downgrade() -> None:
    for column in ("work_directory_id", "session_config_json", "materials_json", "description"):
        op.drop_column("cases", column)
