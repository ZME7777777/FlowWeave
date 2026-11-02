"""store Case control work directories.

Revision ID: 0160_case_control_conversation_directory
Revises: 0159_case_control_plane
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0160_case_control_conversation_directory"
down_revision = "0159_case_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("control_work_directory_id", sa.String(length=36)))
    op.create_unique_constraint(
        "uq_cases_control_work_directory_id", "cases", ["control_work_directory_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_cases_control_work_directory_id", "cases", type_="unique")
    op.drop_column("cases", "control_work_directory_id")
