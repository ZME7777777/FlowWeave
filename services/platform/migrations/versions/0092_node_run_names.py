"""add optional names to single-node run records

Revision ID: 0092_node_run_names
Revises: 0091_skill_collection_latest
"""

import sqlalchemy as sa
from alembic import op

revision = "0092_node_run_names"
down_revision = "0091_skill_collection_latest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("node_runs", sa.Column("name", sa.String(length=220), nullable=True))


def downgrade() -> None:
    op.drop_column("node_runs", "name")
