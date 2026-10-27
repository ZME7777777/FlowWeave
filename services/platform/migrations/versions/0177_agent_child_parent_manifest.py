"""Freeze native parent capability only on newly provisioning bindings."""

import sqlalchemy as sa
from alembic import op

revision = "0177_agent_child_parent_manifest"
down_revision = "0176_agent_child_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_bindings",
        sa.Column("child_parent_manifest_json", sa.JSON(none_as_null=True), nullable=True),
    )
    op.execute("""
CREATE FUNCTION guard_agent_child_parent_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.child_parent_manifest_json IS NOT NULL AND NEW.lifecycle <> 'PROVISIONING' THEN
            RAISE EXCEPTION 'Native parent capability requires initial provisioning'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.child_parent_manifest_json IS NOT NULL THEN
        IF NEW.child_parent_manifest_json::jsonb
           IS DISTINCT FROM OLD.child_parent_manifest_json::jsonb
           OR ROW(NEW.owner_user_id, NEW.host_kind, NEW.host_id, NEW.conversation_scope_id,
                NEW.runtime_session_id, NEW.openhands_conversation_id, NEW.workspace_id,
                NEW.flow_run_id, NEW.working_directory, NEW.work_directory_version_id)
              IS DISTINCT FROM ROW(OLD.owner_user_id, OLD.host_kind, OLD.host_id,
                OLD.conversation_scope_id, OLD.runtime_session_id, OLD.openhands_conversation_id,
                OLD.workspace_id, OLD.flow_run_id, OLD.working_directory,
                OLD.work_directory_version_id)
        THEN
            RAISE EXCEPTION 'Native parent capability and binding identity are immutable'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.child_parent_manifest_json IS NOT NULL AND OLD.lifecycle <> 'PROVISIONING' THEN
        RAISE EXCEPTION 'Cannot enable native parent capability on an existing conversation'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$
""")
    op.execute("""
CREATE TRIGGER tr_agent_child_parent_manifest BEFORE INSERT OR UPDATE ON agent_conversation_bindings
FOR EACH ROW EXECUTE FUNCTION guard_agent_child_parent_manifest()
""")


def downgrade() -> None:
    op.execute("LOCK TABLE agent_conversation_bindings IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security = off")
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM agent_conversation_bindings
               WHERE child_parent_manifest_json IS NOT NULL)
    THEN RAISE EXCEPTION 'Cannot discard frozen native parent capabilities'; END IF;
END $$
""")
    op.execute("DROP TRIGGER tr_agent_child_parent_manifest ON agent_conversation_bindings")
    op.execute("DROP FUNCTION guard_agent_child_parent_manifest()")
    op.drop_column("agent_conversation_bindings", "child_parent_manifest_json")
