"""extend active FlowWeave login sessions to the long-lived policy.

Revision ID: 0152_long_lived_user_sessions
Revises: 0151_stepwise_draft_state
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0152_long_lived_user_sessions"
down_revision = "0151_stepwise_draft_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve only sessions that were valid at deployment; expired credentials
    # still require a new login.
    op.execute(
        sa.text(
            """
            UPDATE user_sessions
            SET expires_at = now() + interval '3650 days'
            WHERE expires_at > now()
            """
        )
    )


def downgrade() -> None:
    # Restoring the prior 12-hour expiry would revoke users unexpectedly.
    pass
