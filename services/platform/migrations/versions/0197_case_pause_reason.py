"""Record why a Case is paused and let the human veto an early conclusion."""

import sqlalchemy as sa
from alembic import op

revision = "0197_case_pause_reason"
down_revision = "0196_case_judgment_actions"
branch_labels = None
depends_on = None

_REASONS = "('MANUAL', 'WAITING_HUMAN', 'CONCLUSION_PROPOSED', 'EXECUTION_CANCELLED')"


def upgrade() -> None:
    op.add_column("cases", sa.Column("pause_reason", sa.String(30), nullable=True))
    op.add_column("cases", sa.Column("pause_note", sa.Text(), nullable=True))
    op.add_column("cases", sa.Column("resume_note", sa.Text(), nullable=True))
    # Backfill from the formal facts that already explain today's PAUSED rows.
    op.execute(
        """
        UPDATE cases c SET pause_reason = CASE
            WHEN EXISTS (
                SELECT 1 FROM case_control_plans p
                WHERE p.case_id = c.id AND p.action = 'ASK_HUMAN' AND p.state = 'SUCCEEDED'
                  AND p.created_at = (
                      SELECT max(q.created_at) FROM case_control_plans q
                      WHERE q.case_id = c.id AND q.state = 'SUCCEEDED')
            ) THEN 'WAITING_HUMAN'
            ELSE 'MANUAL' END
        WHERE c.state = 'PAUSED'
        """
    )
    op.create_check_constraint(
        "ck_case_pause_reason",
        "cases",
        f"pause_reason IS NULL OR (state = 'PAUSED' AND pause_reason IN {_REASONS})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_case_pause_reason", "cases", type_="check")
    op.drop_column("cases", "resume_note")
    op.drop_column("cases", "pause_note")
    op.drop_column("cases", "pause_reason")
