"""User-isolated native child lineage and immutable binding guards."""

import sqlalchemy as sa
from alembic import op

revision = "0176_agent_child_conversations"
down_revision = "0175_add_oh_fw_v3_protocol_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_child_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("host_kind", sa.String(30), nullable=False),
        sa.Column("host_id", sa.String(36), nullable=False),
        sa.Column("conversation_scope_id", sa.String(36), nullable=False),
        sa.Column("runtime_session_id", sa.String(36), nullable=False),
        sa.Column("parent_binding_id", sa.String(36), nullable=False),
        sa.Column("child_binding_id", sa.String(36), nullable=False),
        sa.Column("parent_conversation_id", sa.String(100), nullable=False),
        sa.Column("child_conversation_id", sa.String(100), nullable=False),
        sa.Column("creation_id", sa.String(36), nullable=False),
        sa.Column("action_id", sa.String(200), nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("accepted_generation", sa.Integer(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("child_binding_id", name="uq_agent_child_binding"),
        sa.UniqueConstraint(
            "runtime_session_id", "child_conversation_id", name="uq_agent_child_native"
        ),
        sa.UniqueConstraint("parent_binding_id", "creation_id", name="uq_agent_child_creation"),
        sa.UniqueConstraint(
            "parent_binding_id", "action_id", "tool_call_id", name="uq_agent_child_invocation"
        ),
        sa.CheckConstraint(
            "parent_binding_id <> child_binding_id", name="ck_agent_child_distinct_binding"
        ),
        sa.CheckConstraint(
            "parent_conversation_id <> child_conversation_id", name="ck_agent_child_distinct_native"
        ),
        sa.CheckConstraint(
            "host_kind IN ('AGENT_WORKSPACE', 'FLOW_NODE')", name="ck_agent_child_host"
        ),
        sa.CheckConstraint("accepted_generation >= 1", name="ck_agent_child_generation"),
        sa.CheckConstraint(
            "length(action_id) > 0 AND length(tool_call_id) > 0 "
            "AND manifest_digest ~ '^[0-9a-f]{64}$'",
            name="ck_agent_child_audit",
        ),
    )
    for column in ("owner_user_id", "host_id", "runtime_session_id", "parent_binding_id"):
        op.create_index(
            f"ix_agent_child_conversations_{column}", "agent_child_conversations", [column]
        )
    op.execute("ALTER TABLE agent_child_conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_child_conversations FORCE ROW LEVEL SECURITY")
    policy = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute(
        f"CREATE POLICY pl_tenant_agent_child_conversations ON agent_child_conversations "
        f"USING ({policy}) WITH CHECK ({policy})"
    )
    op.execute("""
CREATE FUNCTION guard_agent_child_relation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    p agent_conversation_bindings%ROWTYPE;
    c agent_conversation_bindings%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Native child lineage is immutable' USING ERRCODE = '23514';
    END IF;
    -- Shared endpoint locks serialize A->B against B->C and binding edits.
    PERFORM id FROM agent_conversation_bindings
        WHERE id IN (NEW.parent_binding_id, NEW.child_binding_id) ORDER BY id FOR UPDATE;
    SELECT * INTO p FROM agent_conversation_bindings WHERE id = NEW.parent_binding_id;
    SELECT * INTO c FROM agent_conversation_bindings WHERE id = NEW.child_binding_id;
    IF p.id IS NULL OR c.id IS NULL
        OR p.owner_user_id IS DISTINCT FROM NEW.owner_user_id
        OR c.owner_user_id IS DISTINCT FROM NEW.owner_user_id
        OR ROW(p.host_kind, p.host_id, p.conversation_scope_id, p.runtime_session_id)
           IS DISTINCT FROM ROW(NEW.host_kind, NEW.host_id,
               NEW.conversation_scope_id, NEW.runtime_session_id)
        OR ROW(c.host_kind, c.host_id, c.conversation_scope_id, c.runtime_session_id)
           IS DISTINCT FROM ROW(NEW.host_kind, NEW.host_id,
               NEW.conversation_scope_id, NEW.runtime_session_id)
        OR p.openhands_conversation_id IS DISTINCT FROM NEW.parent_conversation_id
        OR c.openhands_conversation_id IS DISTINCT FROM NEW.child_conversation_id
        OR p.create_idempotency_key LIKE 'gate-sidecar:%'
        OR p.working_directory IS NULL OR p.working_directory IS DISTINCT FROM c.working_directory
        OR p.workspace_id IS DISTINCT FROM c.workspace_id
        OR p.work_directory_version_id IS DISTINCT FROM c.work_directory_version_id
        OR p.flow_run_id IS DISTINCT FROM c.flow_run_id
        OR c.node_attempt_id IS NOT NULL OR c.node_run_id IS NOT NULL
        OR (NEW.host_kind = 'AGENT_WORKSPACE' AND p.workspace_id IS DISTINCT FROM NEW.host_id)
        OR (NEW.host_kind = 'FLOW_NODE' AND p.flow_run_id IS DISTINCT FROM NEW.host_id)
        OR EXISTS (SELECT 1 FROM agent_sidebar_conversations
            WHERE sidebar_binding_id IN (p.id, c.id))
        OR EXISTS (SELECT 1 FROM agent_child_conversations
            WHERE child_binding_id = p.id OR parent_binding_id = c.id)
    THEN
        RAISE EXCEPTION 'Invalid direct native child binding identity' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$
""")
    op.execute("""
CREATE TRIGGER tr_agent_child_relation BEFORE INSERT OR UPDATE ON agent_child_conversations
FOR EACH ROW EXECUTE FUNCTION guard_agent_child_relation()
""")
    op.execute("""
CREATE FUNCTION guard_agent_child_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_child_conversations
        WHERE parent_binding_id = OLD.id OR child_binding_id = OLD.id)
    THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Remove native child lineage explicitly before binding deletion'
                USING ERRCODE = '23514';
        END IF;
        IF ROW(NEW.owner_user_id, NEW.host_kind, NEW.host_id, NEW.conversation_scope_id,
            NEW.runtime_session_id, NEW.openhands_conversation_id, NEW.workspace_id,
            NEW.flow_run_id, NEW.node_run_id, NEW.node_attempt_id,
            NEW.working_directory, NEW.work_directory_version_id)
        IS DISTINCT FROM ROW(OLD.owner_user_id, OLD.host_kind, OLD.host_id,
            OLD.conversation_scope_id,
            OLD.runtime_session_id, OLD.openhands_conversation_id, OLD.workspace_id,
            OLD.flow_run_id, OLD.node_run_id, OLD.node_attempt_id,
            OLD.working_directory, OLD.work_directory_version_id)
        THEN
            RAISE EXCEPTION 'Native child binding identity is immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$
""")
    op.execute("""
CREATE TRIGGER tr_agent_child_binding BEFORE UPDATE OR DELETE ON agent_conversation_bindings
FOR EACH ROW EXECUTE FUNCTION guard_agent_child_binding()
""")


def downgrade() -> None:
    op.execute("LOCK TABLE agent_child_conversations IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security = off")
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM agent_child_conversations) THEN
        RAISE EXCEPTION 'Cannot downgrade while native child relations exist';
    END IF;
END $$
""")
    op.execute("DROP TRIGGER tr_agent_child_binding ON agent_conversation_bindings")
    op.execute("DROP FUNCTION guard_agent_child_binding()")
    op.drop_table("agent_child_conversations")
    op.execute("DROP FUNCTION guard_agent_child_relation()")
