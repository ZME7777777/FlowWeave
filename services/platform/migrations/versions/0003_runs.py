"""run snapshots and attempts"""

import sqlalchemy as sa
from alembic import op

revision = "0003_runs"
down_revision = "0002_flows"
branch_labels = None
depends_on = None


def _create_flow_run() -> None:
    # This is the original 0003 schema. Later revisions own environment,
    # Lark, automation, scheduling and tenancy fields; never read the live ORM.
    op.create_table(
        "flow_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_definition_id",
            sa.String(36),
            sa.ForeignKey("flow_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_no", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(220), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("active_snapshot_id", sa.String(36), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("completion_mode", sa.String(10), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("flow_definition_id", "run_no", name="uq_flow_run_number"),
    )
    op.create_index("ix_flow_runs_flow_definition_id", "flow_runs", ["flow_definition_id"])


def _create_node_run() -> None:
    # Freeze the schema before 0092 adds name. Reading the live ORM here
    # creates future columns early and breaks the incremental migration chain.
    op.create_table(
        "node_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("flow_run_id", sa.String(36), nullable=False),
        sa.Column("flow_node_snapshot_key", sa.String(100), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("accepted_attempt_id", sa.String(36), nullable=True),
        sa.Column("created_from", sa.String(30), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("flow_run_id", "sequence_no", name="uq_run_node_sequence"),
    )
    op.create_index("ix_node_runs_flow_run_id", "node_runs", ["flow_run_id"])


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


def _create_human_action() -> None:
    op.create_table(
        "human_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_run_id",
            sa.String(36),
            sa.ForeignKey("flow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_run_id",
            sa.String(36),
            sa.ForeignKey("node_runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("action_type", sa.String(60), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("flow_run_id", "node_run_id", "attempt_id"):
        op.create_index(f"ix_human_actions_{column}", "human_actions", [column])


def upgrade():
    _create_flow_run()
    _create_run_snapshot()
    _create_node_run()
    _create_node_attempt()
    _create_human_action()


def downgrade():
    op.drop_table("human_actions")
    op.drop_table("node_attempts")
    op.drop_table("node_runs")
    op.drop_table("run_snapshots")
    op.drop_table("flow_runs")
