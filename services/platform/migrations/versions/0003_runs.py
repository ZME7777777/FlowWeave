"""run snapshots and attempts"""

import sqlalchemy as sa
from alembic import op

from flowweave.modules.catalog.infrastructure import models as catalog_models  # noqa: F401
from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.modules.model_providers.infrastructure import models as provider_models  # noqa: F401
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.modules.tasks.infrastructure import models as task_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0003_runs"
down_revision = "0002_flows"
branch_labels = None
depends_on = None
TABLES_BEFORE_SNAPSHOT = ["flow_runs"]
TABLES_BEFORE_ATTEMPT = ["node_runs"]
TABLES_AFTER_ATTEMPT = ["human_actions"]


def _create_run_snapshot() -> None:
    op.create_table(
        "run_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_run_id",
            sa.String(36),
            sa.ForeignKey("flow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("runtime_manifest_json", sa.JSON(), nullable=False),
        sa.Column("runtime_manifest_hash", sa.String(64), nullable=False),
        sa.Column("created_by_action_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("flow_run_id", "version", name="uq_snapshot_version"),
    )
    op.create_index("ix_run_snapshots_flow_run_id", "run_snapshots", ["flow_run_id"])


def _create_node_attempt() -> None:
    op.create_table(
        "node_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "node_run_id",
            sa.String(36),
            sa.ForeignKey("node_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("run_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("runtime_phase", sa.String(30), nullable=True),
        sa.Column("runtime_adapter", sa.String(30), nullable=True),
        sa.Column("runtime_job_id", sa.String(100), nullable=True),
        sa.Column("conversation_id", sa.String(100), nullable=True),
        sa.Column("runtime_cursor", sa.String(200), nullable=True),
        sa.Column("runtime_sandbox_id", sa.String(36), nullable=True),
        sa.Column("workspace_ref", sa.Text(), nullable=True),
        sa.Column("startup_mode", sa.String(20), nullable=False),
        sa.Column("startup_capability_key", sa.String(200), nullable=True),
        sa.Column("startup_prompt", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(240), nullable=True),
        sa.Column("reasoning_effort", sa.String(30), nullable=True),
        sa.Column("confirmation_policy", sa.String(20), nullable=False),
        sa.Column("condenser_config_json", sa.JSON(), nullable=False),
        sa.Column("output_targets_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_run_id", "attempt_no", name="uq_node_attempt_number"),
        sa.CheckConstraint(
            "confirmation_policy IN ('ALWAYS', 'NEVER')",
            name="ck_attempt_confirmation_policy",
        ),
    )
    op.create_index("ix_node_attempts_node_run_id", "node_attempts", ["node_run_id"])
    op.create_index("ix_node_attempts_snapshot_id", "node_attempts", ["snapshot_id"])
    op.create_index(
        "ix_node_attempts_runtime_sandbox_id",
        "node_attempts",
        ["runtime_sandbox_id"],
    )


def upgrade():
    bind = op.get_bind()
    for name in TABLES_BEFORE_SNAPSHOT:
        Base.metadata.tables[name].create(bind, checkfirst=True)
    _create_run_snapshot()
    for name in TABLES_BEFORE_ATTEMPT:
        Base.metadata.tables[name].create(bind, checkfirst=True)
    _create_node_attempt()
    for name in TABLES_AFTER_ATTEMPT:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES_AFTER_ATTEMPT):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
    op.drop_table("node_attempts")
    for name in reversed(TABLES_BEFORE_ATTEMPT):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
    op.drop_table("run_snapshots")
    for name in reversed(TABLES_BEFORE_SNAPSHOT):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
