"""Freeze governed Runtime image capabilities on Environment Versions.

Revision ID: 0111_environment_runtime_capabilities
Revises: 0110_candidate_output_set_owner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0111_environment_runtime_capabilities"
down_revision = "0110_candidate_output_set_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environment_versions",
        sa.Column(
            "runtime_capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.alter_column("environment_versions", "runtime_capabilities", server_default=None)


def downgrade() -> None:
    op.drop_column("environment_versions", "runtime_capabilities")
