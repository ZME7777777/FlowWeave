"""restrict event trigger actions to non-mutating observers

Revision ID: 0102_event_trigger_observers
Revises: 0101_event_trigger_deliveries
"""

from __future__ import annotations

from alembic import op

revision = "0102_event_trigger_observers"
down_revision = "0101_event_trigger_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Disable every historical trigger that requested a FlowWeave-side
    # Conversation/Runtime mutation.  Preserve the row for audit, but retire
    # its executable type before tightening the database constraint.
    op.execute(
        "UPDATE event_trigger_versions SET enabled = false "
        "WHERE id IN (SELECT trigger_version_id FROM event_trigger_actions "
        "WHERE action_type NOT IN ('WEBHOOK', 'NOTIFY', 'CREATE_TASK'))"
    )
    op.execute(
        "UPDATE event_trigger_actions SET action_type = 'NOTIFY', "
        "description = '[RETIRED_NON_MUTATING_ACTION] ' || description "
        "WHERE action_type NOT IN ('WEBHOOK', 'NOTIFY', 'CREATE_TASK')"
    )
    op.drop_constraint(
        "ck_event_trigger_action_type", "event_trigger_actions", type_="check"
    )
    op.create_check_constraint(
        "ck_event_trigger_action_type",
        "event_trigger_actions",
        "action_type IN ('WEBHOOK', 'NOTIFY', 'CREATE_TASK')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_event_trigger_action_type", "event_trigger_actions", type_="check"
    )
    op.create_check_constraint(
        "ck_event_trigger_action_type",
        "event_trigger_actions",
        "action_type IN ('RESUME_CONVERSATION', 'WEBHOOK', 'NOTIFY', 'CREATE_TASK', "
        "'PAUSE_ATTEMPT', 'HANDOFF_HUMAN')",
    )
