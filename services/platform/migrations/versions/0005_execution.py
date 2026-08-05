"""execution, task queue, events"""

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


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
