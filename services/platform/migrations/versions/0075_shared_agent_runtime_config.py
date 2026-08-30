"""Move Agent runtime configuration from nodes and attempts to shared sessions.

Revision ID: 0075_shared_agent_runtime_config
Revises: 0074_flow_run_work_directories
"""

import sqlalchemy as sa
from alembic import op

revision = "0075_shared_agent_runtime_config"
down_revision = "0074_flow_run_work_directories"
branch_labels = None
depends_on = None


_NODE_EXECUTOR_COLUMNS = (
    "model_provider_id",
    "model_name",
    "timeout_seconds",
    "max_iterations",
    "confirmation_policy",
    "condenser_config_json",
)
_ATTEMPT_COLUMNS = (
    "model_name",
    "reasoning_effort",
    "confirmation_policy",
    "condenser_config_json",
)


def upgrade() -> None:
    bind = op.get_bind()
    executor_constraints = {
        item["name"] for item in sa.inspect(bind).get_check_constraints("node_executor_configs")
    }
    for name in (
        "ck_executor_timeout_positive",
        "ck_executor_iterations_positive",
        "ck_executor_confirmation_policy",
    ):
        if name in executor_constraints:
            op.drop_constraint(name, "node_executor_configs", type_="check")

    attempt_constraints = {
        item["name"] for item in sa.inspect(bind).get_check_constraints("node_attempts")
    }
    if "ck_attempt_confirmation_policy" in attempt_constraints:
        op.drop_constraint("ck_attempt_confirmation_policy", "node_attempts", type_="check")

    for column in _NODE_EXECUTOR_COLUMNS:
        op.drop_column("node_executor_configs", column)
    op.drop_table("node_capability_refs")
    for column in _ATTEMPT_COLUMNS:
        op.drop_column("node_attempts", column)


def downgrade() -> None:
    op.add_column(
        "node_attempts",
        sa.Column(
            "condenser_config_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text('\'{"kind": "LLM_SUMMARIZING"}\'::json'),
        ),
    )
    op.add_column(
        "node_attempts",
        sa.Column("confirmation_policy", sa.String(20), nullable=False, server_default="NEVER"),
    )
    op.add_column("node_attempts", sa.Column("reasoning_effort", sa.String(30), nullable=True))
    op.add_column("node_attempts", sa.Column("model_name", sa.String(240), nullable=True))
    op.create_check_constraint(
        "ck_attempt_confirmation_policy",
        "node_attempts",
        "confirmation_policy IN ('ALWAYS', 'NEVER')",
    )

    op.create_table(
        "node_capability_refs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "node_asset_id",
            sa.String(36),
            sa.ForeignKey("node_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability_type", sa.String(16), nullable=False),
        sa.Column("capability_key", sa.String(200), nullable=False),
        sa.Column(
            "capability_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "normalized_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "node_asset_id", "capability_type", "capability_key", name="uq_asset_capability"
        ),
    )
    op.create_index(
        "ix_node_capability_refs_node_asset_id", "node_capability_refs", ["node_asset_id"]
    )
    op.create_index(
        "ix_node_capability_refs_capability_version_id",
        "node_capability_refs",
        ["capability_version_id"],
    )

    op.add_column(
        "node_executor_configs",
        sa.Column(
            "condenser_config_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text('\'{"kind": "LLM_SUMMARIZING"}\'::json'),
        ),
    )
    op.add_column(
        "node_executor_configs",
        sa.Column("confirmation_policy", sa.String(20), nullable=False, server_default="NEVER"),
    )
    op.add_column(
        "node_executor_configs",
        sa.Column("max_iterations", sa.Integer(), nullable=False, server_default="100"),
    )
    op.add_column(
        "node_executor_configs",
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="900"),
    )
    op.add_column("node_executor_configs", sa.Column("model_name", sa.String(200), nullable=True))
    op.add_column(
        "node_executor_configs",
        sa.Column(
            "model_provider_id",
            sa.String(36),
            sa.ForeignKey("model_providers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_executor_timeout_positive", "node_executor_configs", "timeout_seconds > 0"
    )
    op.create_check_constraint(
        "ck_executor_iterations_positive", "node_executor_configs", "max_iterations > 0"
    )
    op.create_check_constraint(
        "ck_executor_confirmation_policy",
        "node_executor_configs",
        "confirmation_policy IN ('ALWAYS', 'NEVER')",
    )
