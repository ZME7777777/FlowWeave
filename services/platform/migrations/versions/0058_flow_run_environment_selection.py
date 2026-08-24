"""Move Environment Version selection from Flow Definition to FlowRun start.

Revision ID: 0058_run_environment
Revises: 0057_flow_run_conversations
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0058_run_environment"
down_revision = "0057_flow_run_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_flow_definitions_environment_version_id", table_name="flow_definitions")
    op.drop_constraint(
        "fk_flow_definitions_environment_version",
        "flow_definitions",
        type_="foreignkey",
    )
    op.drop_column("flow_definitions", "environment_version_id")


def downgrade() -> None:
    op.add_column(
        "flow_definitions",
        sa.Column("environment_version_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_flow_definitions_environment_version",
        "flow_definitions",
        "environment_versions",
        ["environment_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_flow_definitions_environment_version_id",
        "flow_definitions",
        ["environment_version_id"],
    )
