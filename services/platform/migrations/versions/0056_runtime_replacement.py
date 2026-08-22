"""add durable FlowRun Runtime replacement lease and target generation

Revision ID: 0056_runtime_replacement
Revises: 0055_conversation_bindings

The replacement fields are empty for existing Runtime Sessions. A replacement
worker freezes and fills them transactionally before it creates N+1, so legacy
or partially migrated sessions never infer a recovery target.
"""

import sqlalchemy as sa
from alembic import op

revision = "0056_runtime_replacement"
down_revision = "0055_conversation_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_lease_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_lease_owner", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_error_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "flow_run_runtimes",
        sa.Column("replacement_error_summary", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_flow_run_runtimes_replacement_lease_token",
        "flow_run_runtimes",
        ["replacement_lease_token"],
    )
    op.create_check_constraint(
        "ck_flow_run_runtime_replacement_generation",
        "flow_run_runtimes",
        "replacement_generation IS NULL OR replacement_generation >= 1",
    )
    op.create_check_constraint(
        "ck_flow_run_runtime_replacement_order",
        "flow_run_runtimes",
        "replacement_generation IS NULL OR active_generation IS NULL "
        "OR replacement_generation > active_generation",
    )
    op.create_check_constraint(
        "ck_flow_run_runtime_replacement_lease",
        "flow_run_runtimes",
        "(replacement_lease_token IS NULL AND replacement_lease_owner IS NULL "
        "AND replacement_lease_until IS NULL) OR "
        "(replacement_lease_token IS NOT NULL AND replacement_lease_owner IS NOT NULL "
        "AND replacement_lease_until IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_flow_run_runtime_replacement_generation",
        "flow_run_runtimes",
        "runtime_generations",
        ["id", "replacement_generation"],
        ["runtime_session_id", "generation"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_flow_run_runtime_replacement_generation",
        "flow_run_runtimes",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_flow_run_runtime_replacement_lease",
        "flow_run_runtimes",
        type_="check",
    )
    op.drop_constraint(
        "ck_flow_run_runtime_replacement_order",
        "flow_run_runtimes",
        type_="check",
    )
    op.drop_constraint(
        "ck_flow_run_runtime_replacement_generation",
        "flow_run_runtimes",
        type_="check",
    )
    op.drop_constraint(
        "uq_flow_run_runtimes_replacement_lease_token",
        "flow_run_runtimes",
        type_="unique",
    )
    op.drop_column("flow_run_runtimes", "replacement_error_summary")
    op.drop_column("flow_run_runtimes", "replacement_error_code")
    op.drop_column("flow_run_runtimes", "replacement_not_before")
    op.drop_column("flow_run_runtimes", "replacement_started_at")
    op.drop_column("flow_run_runtimes", "replacement_lease_until")
    op.drop_column("flow_run_runtimes", "replacement_lease_owner")
    op.drop_column("flow_run_runtimes", "replacement_lease_token")
    op.drop_column("flow_run_runtimes", "replacement_generation")
