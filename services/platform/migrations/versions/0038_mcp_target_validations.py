"""project target-environment MCP validation results

Revision ID: 0038_mcp_target_validations
Revises: 0037_subagent_task_usage
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_mcp_target_validations"
down_revision = "0037_subagent_task_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_capability_validation_status", "capability_validations", type_="check")
    op.create_check_constraint(
        "ck_capability_validation_status",
        "capability_validations",
        "status IN ('RUNNING', 'PASSED', 'FAILED')",
    )
    op.add_column(
        "capability_validations",
        sa.Column("environment_version_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "capability_validations",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_capability_validation_environment_version",
        "capability_validations",
        "environment_versions",
        ["environment_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_capability_validations_environment_version_id",
        "capability_validations",
        ["environment_version_id"],
    )


def downgrade() -> None:
    op.execute(
        "UPDATE capability_validations SET status = 'FAILED', "
        "completed_at = COALESCE(completed_at, created_at) WHERE status = 'RUNNING'"
    )
    op.drop_index(
        "ix_capability_validations_environment_version_id",
        table_name="capability_validations",
    )
    op.drop_constraint(
        "fk_capability_validation_environment_version",
        "capability_validations",
        type_="foreignkey",
    )
    op.drop_column("capability_validations", "completed_at")
    op.drop_column("capability_validations", "environment_version_id")
    op.drop_constraint("ck_capability_validation_status", "capability_validations", type_="check")
    op.create_check_constraint(
        "ck_capability_validation_status",
        "capability_validations",
        "status IN ('PASSED', 'FAILED')",
    )
