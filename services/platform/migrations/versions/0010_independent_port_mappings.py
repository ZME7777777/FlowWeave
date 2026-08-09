"""separate port mappings from flow direction edges"""

from alembic import op

from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0010_independent_port_mappings"
down_revision = "0009_runtime_cancellation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["flow_port_mappings"].create(bind, checkfirst=True)
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
