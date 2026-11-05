"""Let one API-key provider serve several wire protocols, chosen per model."""

import sqlalchemy as sa
from alembic import op

revision = "0198_provider_model_protocol"
down_revision = "0197_case_pause_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_models", sa.Column("protocol", sa.String(30), nullable=True))
    # Freeze today's behaviour explicitly so name-based inference never changes
    # the protocol of an already configured model.
    op.execute(
        """
        UPDATE provider_models pm SET protocol = CASE
            WHEN p.auth_type = 'ANTHROPIC_API_KEY' THEN 'ANTHROPIC'
            WHEN p.api_protocol = 'RESPONSES' THEN 'RESPONSES'
            ELSE 'CHAT_COMPLETIONS' END
        FROM model_providers p
        WHERE pm.provider_id = p.id AND p.auth_type <> 'CODEX_OAUTH'
        """
    )
    op.execute(
        "UPDATE model_providers SET auth_type = 'API_KEY' WHERE auth_type = 'ANTHROPIC_API_KEY'"
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE model_providers p SET auth_type = 'ANTHROPIC_API_KEY'
        WHERE p.auth_type = 'API_KEY'
          AND EXISTS (SELECT 1 FROM provider_models m WHERE m.provider_id = p.id)
          AND NOT EXISTS (
              SELECT 1 FROM provider_models m
              WHERE m.provider_id = p.id AND m.protocol IS DISTINCT FROM 'ANTHROPIC')
        """
    )
    op.drop_column("provider_models", "protocol")
