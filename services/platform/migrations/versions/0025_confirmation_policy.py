"""freeze native OpenHands confirmation policy

Revision ID: 0025_confirmation_policy
Revises: 0024_confirmation_batches
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_confirmation_policy"
down_revision = "0024_confirmation_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name, constraint_name in (
        ("node_executor_configs", "ck_executor_confirmation_policy"),
        ("node_attempts", "ck_attempt_confirmation_policy"),
    ):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "confirmation_policy" not in columns:
            op.add_column(
                table_name,
                sa.Column(
                    "confirmation_policy",
                    sa.String(20),
                    nullable=False,
                    server_default="ALWAYS",
                ),
            )
        constraints = {
            constraint["name"] for constraint in sa.inspect(bind).get_check_constraints(table_name)
        }
        if constraint_name not in constraints:
            op.create_check_constraint(
                constraint_name,
                table_name,
                "confirmation_policy IN ('ALWAYS', 'NEVER')",
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name, constraint_name in (
        ("node_attempts", "ck_attempt_confirmation_policy"),
        ("node_executor_configs", "ck_executor_confirmation_policy"),
    ):
        constraints = {
            constraint["name"] for constraint in sa.inspect(bind).get_check_constraints(table_name)
        }
        if constraint_name in constraints:
            op.drop_constraint(constraint_name, table_name, type_="check")
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "confirmation_policy" in columns:
            op.drop_column(table_name, "confirmation_policy")
