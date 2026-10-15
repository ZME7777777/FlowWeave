"""freeze the static OpenHands protocol profile for FlowRun Runtimes.

Revision ID: 0147_flow_run_runtime_protocol_profile
Revises: 0146_shared_flow_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0147_flow_run_runtime_protocol_profile"
down_revision = "0146_shared_flow_runs"
branch_labels = None
depends_on = None

_DEFAULT_PROFILE = "OH_FW_V1"


def upgrade() -> None:
    for table in ("flow_run_runtimes", "runtime_generations"):
        op.add_column(
            table,
            sa.Column(
                "protocol_profile",
                sa.String(length=40),
                nullable=True,
                server_default=sa.text(f"'{_DEFAULT_PROFILE}'"),
            ),
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET protocol_profile = :profile WHERE protocol_profile IS NULL"
            ).bindparams(profile=_DEFAULT_PROFILE)
        )
        op.alter_column(table, "protocol_profile", nullable=False, server_default=None)

    op.create_check_constraint(
        "ck_flow_run_runtime_protocol_profile",
        "flow_run_runtimes",
        "protocol_profile IN ('OH_FW_V1')",
    )
    op.create_check_constraint(
        "ck_runtime_generation_protocol_profile",
        "runtime_generations",
        "protocol_profile IN ('OH_FW_V1')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_runtime_generation_protocol_profile",
        "runtime_generations",
        type_="check",
    )
    op.drop_constraint(
        "ck_flow_run_runtime_protocol_profile",
        "flow_run_runtimes",
        type_="check",
    )
    op.drop_column("runtime_generations", "protocol_profile")
    op.drop_column("flow_run_runtimes", "protocol_profile")
