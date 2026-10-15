"""freeze the FlowRun protocol profile on Environment Versions.

Revision ID: 0148_environment_runtime_protocol_profile
Revises: 0147_flow_run_runtime_protocol_profile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0148_environment_runtime_protocol_profile"
down_revision = "0147_flow_run_runtime_protocol_profile"
branch_labels = None
depends_on = None

_DEFAULT_PROFILE = "OH_FW_V1"


def upgrade() -> None:
    op.add_column(
        "environment_versions",
        sa.Column(
            "runtime_protocol_profile",
            sa.String(length=40),
            nullable=True,
            server_default=sa.text(f"'{_DEFAULT_PROFILE}'"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE environment_versions SET runtime_protocol_profile = :profile "
            "WHERE runtime_protocol_profile IS NULL"
        ).bindparams(profile=_DEFAULT_PROFILE)
    )
    op.alter_column(
        "environment_versions",
        "runtime_protocol_profile",
        nullable=False,
        server_default=None,
    )
    op.create_check_constraint(
        "ck_environment_version_runtime_protocol_profile",
        "environment_versions",
        "runtime_protocol_profile IN ('OH_FW_V1')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_environment_version_runtime_protocol_profile",
        "environment_versions",
        type_="check",
    )
    op.drop_column("environment_versions", "runtime_protocol_profile")
