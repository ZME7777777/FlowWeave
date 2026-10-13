"""flow definitions"""

import sqlalchemy as sa
from alembic import op

from flowweave.modules.catalog.infrastructure import models as catalog_models  # noqa: F401
from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.modules.model_providers.infrastructure import models as provider_models  # noqa: F401
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.modules.tasks.infrastructure import models as task_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0002_flows"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None
TABLES = ["flow_nodes", "flow_edges", "gate_policies"]


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


def upgrade():
    bind = op.get_bind()
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
        if name == "flow_edges":
            _create_flow_edge()
        else:
            Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        if name == "flow_edges":
            op.drop_table("flow_edges")
        else:
            Base.metadata.tables[name].drop(bind, checkfirst=True)
    op.drop_table("flow_definitions")
