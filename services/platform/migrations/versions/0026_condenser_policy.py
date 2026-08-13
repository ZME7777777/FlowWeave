"""freeze native OpenHands condenser policy

Revision ID: 0026_condenser_policy
Revises: 0025_confirmation_policy
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0026_condenser_policy"
down_revision = "0025_confirmation_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    default = sa.text('\'{"kind": "NO_OP"}\'::json')
    for table_name in ("node_executor_configs", "node_attempts"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "condenser_config_json" not in columns:
            op.add_column(
                table_name,
                sa.Column(
                    "condenser_config_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=default,
                ),
            )

        # Historical baseline migrations build tables from current ORM metadata.
        # Normalize both old databases and fresh installs to an explicit policy.
        bind.execute(
            sa.text(
                f"UPDATE {table_name} "
                "SET condenser_config_json = CAST(:value AS JSON) "
                "WHERE condenser_config_json IS NULL"
            ),
            {"value": json.dumps({"kind": "NO_OP"})},
        )
        op.alter_column(table_name, "condenser_config_json", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("node_attempts", "node_executor_configs"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "condenser_config_json" in columns:
            op.drop_column(table_name, "condenser_config_json")
