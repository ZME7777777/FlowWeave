"""execution, task queue, events"""

import sqlalchemy as sa
from alembic import op

from flowweave.modules.catalog.infrastructure import models as catalog_models  # noqa: F401
from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.modules.model_providers.infrastructure import models as provider_models  # noqa: F401
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.modules.tasks.infrastructure import models as task_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0005_execution"
down_revision = "0004_artifacts"
branch_labels = None
depends_on = None
TABLES = ["gate_evaluations", "background_tasks", "run_events"]


def _create_gate_evaluation() -> None:
    # Original audit record; 0095 adds owner and 0104 adds candidate output.
    op.create_table(
        "gate_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("node_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_snapshot_key", sa.String(100), nullable=False),
        sa.Column("stage", sa.String(10), nullable=False),
        sa.Column("policy_position", sa.Integer(), nullable=False),
        sa.Column("evaluation_attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("log_excerpt", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "attempt_id",
            "policy_snapshot_key",
            "stage",
            "evaluation_attempt",
            name="uq_gate_evaluation_attempt",
        ),
    )
    op.create_index("ix_gate_evaluations_attempt_id", "gate_evaluations", ["attempt_id"])


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        if name == "gate_evaluations":
            _create_gate_evaluation()
        else:
            Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        if name == "gate_evaluations":
            op.drop_table(name)
        else:
            Base.metadata.tables[name].drop(bind, checkfirst=True)
