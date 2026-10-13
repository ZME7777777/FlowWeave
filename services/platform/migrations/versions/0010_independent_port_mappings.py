"""separate port mappings from flow direction edges"""

import sqlalchemy as sa
from alembic import op

revision = "0010_independent_port_mappings"
down_revision = "0009_runtime_cancellation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Original mapping schema; 0095 introduces tenant ownership.
    op.create_table(
        "flow_port_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_id",
            sa.String(36),
            sa.ForeignKey("flow_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_flow_node_id",
            sa.String(36),
            sa.ForeignKey("flow_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_output_key", sa.String(100), nullable=False),
        sa.Column(
            "target_flow_node_id",
            sa.String(36),
            sa.ForeignKey("flow_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_input_key", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "flow_id",
            "target_flow_node_id",
            "target_input_key",
            name="uq_flow_target_input_mapping",
        ),
    )
    op.create_index("ix_flow_port_mappings_flow_id", "flow_port_mappings", ["flow_id"])
    op.execute("DROP TABLE IF EXISTS flow_edge_mappings")
    op.execute("ALTER TABLE node_assets DROP COLUMN IF EXISTS default_skill_ref")
    op.execute(
        "ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS startup_mode VARCHAR(20) "
        "NOT NULL DEFAULT 'PROMPT'"
    )
    op.execute(
        "ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS startup_capability_key VARCHAR(200)"
    )
    op.execute("ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS startup_prompt TEXT")


def downgrade() -> None:
    op.drop_table("flow_port_mappings")
