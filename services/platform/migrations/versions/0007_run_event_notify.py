"""run event LISTEN/NOTIFY wakeups"""

from alembic import op

revision = "0007_run_event_notify"
down_revision = "0006_capability_imports"
branch_labels = None
depends_on = None

_FUNCTION = "flowweave_notify_run_event"
_TRIGGER = "trg_run_events_notify"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('flowweave_run_events', NEW.flow_run_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        AFTER INSERT ON run_events
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON run_events")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
