"""retain terminal environment version number high-water marks"""

import sqlalchemy as sa
from alembic import op

revision = "0016_env_version_delete"
down_revision = "0015_run_terminal_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("terminal_environments")}
    if "last_version_no" not in columns:
        op.add_column(
            "terminal_environments",
            sa.Column("last_version_no", sa.Integer(), nullable=False, server_default="0"),
        )
    op.execute(
        sa.text(
            """
            UPDATE terminal_environments AS environment
            SET last_version_no = GREATEST(
                environment.last_version_no,
                COALESCE((
                    SELECT MAX(version_no)
                    FROM environment_versions AS version
                    WHERE version.environment_id = environment.id
                ), 0)
            )
            """
        )
    )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("terminal_environments")
    }
    if "last_version_no" in columns:
        op.drop_column("terminal_environments", "last_version_no")
