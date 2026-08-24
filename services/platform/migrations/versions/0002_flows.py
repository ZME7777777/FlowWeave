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
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
    op.drop_table("flow_definitions")
