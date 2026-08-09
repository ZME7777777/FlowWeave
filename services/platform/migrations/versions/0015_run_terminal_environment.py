"""bind flow runs to immutable terminal environment versions"""

import sqlalchemy as sa
from alembic import op

revision = "0015_run_terminal_environment"
down_revision = "0014_terminal_environments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("flow_runs")}
    if "environment_version_id" not in columns:
        op.add_column(
            "flow_runs",
            sa.Column("environment_version_id", sa.String(length=36), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("flow_runs")}
    if "ix_flow_runs_environment_version_id" not in indexes:
        op.create_index(
            "ix_flow_runs_environment_version_id",
            "flow_runs",
            ["environment_version_id"],
        )
    foreign_keys = {
        foreign_key["name"] for foreign_key in sa.inspect(bind).get_foreign_keys("flow_runs")
    }
    if "fk_flow_runs_environment_version" not in foreign_keys:
        op.create_foreign_key(
            "fk_flow_runs_environment_version",
            "flow_runs",
            "environment_versions",
            ["environment_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    foreign_keys = {
        foreign_key["name"] for foreign_key in sa.inspect(bind).get_foreign_keys("flow_runs")
    }
    if "fk_flow_runs_environment_version" in foreign_keys:
        op.drop_constraint("fk_flow_runs_environment_version", "flow_runs", type_="foreignkey")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("flow_runs")}
    if "ix_flow_runs_environment_version_id" in indexes:
        op.drop_index("ix_flow_runs_environment_version_id", table_name="flow_runs")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("flow_runs")}
    if "environment_version_id" in columns:
        op.drop_column("flow_runs", "environment_version_id")
