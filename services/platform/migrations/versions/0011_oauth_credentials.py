"""add OAuth credential connections and runtime leases"""

from alembic import op

from flowweave.modules.credentials.infrastructure import models as credential_models  # noqa: F401
from flowweave.shared.database import Base

revision = "0011_oauth_credentials"
down_revision = "0010_independent_port_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("oauth_sessions", "credential_connections", "credential_leases"):
        Base.metadata.tables[table_name].create(bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("credential_leases")
    op.drop_table("credential_connections")
    op.drop_table("oauth_sessions")
