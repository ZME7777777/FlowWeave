"""flow definitions"""

import sqlalchemy as sa
from alembic import op

revision = "0002_flows"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None
TABLES = ["flow_nodes", "flow_edges", "gate_policies"]


def _create_flow_node() -> None:
    # Original node instance fields; tenant ownership belongs to 0095.
    op.create_table(
        "flow_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_id",
            sa.String(36),
            sa.ForeignKey("flow_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instance_key", sa.String(100), nullable=False),
        sa.Column(
            "node_asset_id",
            sa.String(36),
            sa.ForeignKey("node_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(200), nullable=True),
        sa.Column("position_x", sa.Integer(), nullable=False),
        sa.Column("position_y", sa.Integer(), nullable=False),
        sa.Column("config_override", sa.JSON(), nullable=False),
        sa.UniqueConstraint("flow_id", "instance_key", name="uq_flow_instance_key"),
    )
    op.create_index("ix_flow_nodes_flow_id", "flow_nodes", ["flow_id"])


def _create_flow_edge() -> None:
    # Freeze the original control edge; 0095 owns its tenant column.
    op.create_table(
        "flow_edges",
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
        sa.Column(
            "target_flow_node_id",
            sa.String(36),
            sa.ForeignKey("flow_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "flow_id",
            "source_flow_node_id",
            "target_flow_node_id",
            "position",
            name="uq_flow_edge_position",
        ),
    )
    op.create_index("ix_flow_edges_flow_id", "flow_edges", ["flow_id"])


def _create_gate_policy() -> None:
    op.create_table(
        "gate_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "flow_node_id",
            sa.String(36),
            sa.ForeignKey("flow_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(10), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("gate_type", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("flow_node_id", "stage", "position", name="uq_gate_stage_position"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_gate_timeout_positive"),
    )
    op.create_index("ix_gate_policies_flow_node_id", "gate_policies", ["flow_node_id"])


def upgrade():
    # Keep the historical baseline independent from current ORM fields.  In
    # particular, FR-01 adds the Environment FK in 0052, after the referenced
    # table is introduced by 0014.
    op.create_table(
        "flow_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("default_entry_key", sa.String(100), nullable=True),
        sa.Column("lark_root_folder_url", sa.Text(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in TABLES:
        if name == "flow_nodes":
            _create_flow_node()
        elif name == "flow_edges":
            _create_flow_edge()
        else:
            _create_gate_policy()


def downgrade():
    for name in reversed(TABLES):
        op.drop_table(name)
    op.drop_table("flow_definitions")
