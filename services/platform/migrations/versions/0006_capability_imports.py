"""persistent capability import sessions"""

from alembic import op

from flowweave.modules.catalog.infrastructure import models as catalog_models  # noqa: F401
from flowweave.modules.flows.infrastructure import models as flow_models  # noqa: F401
from flowweave.modules.model_providers.infrastructure import models as provider_models  # noqa: F401
from flowweave.modules.runs.infrastructure import models as run_models  # noqa: F401
from flowweave.modules.tasks.infrastructure import models as task_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0006_capability_imports"
down_revision = "0005_execution"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.tables["capability_imports"].create(op.get_bind(), checkfirst=True)


def downgrade():
    Base.metadata.tables["capability_imports"].drop(op.get_bind(), checkfirst=True)
