"""Freeze child creation governance without storing tasks or credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0179_agent_child_creation_audit"
down_revision = "0178_add_oh_fw_v4_protocol_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, length in (
        ("creation_policy_version", 50),
        ("request_digest", 64),
        ("expected_agent_digest", 64),
        ("expected_persistence_dir", 1000),
    ):
        op.add_column(
            "agent_child_conversations", sa.Column(name, sa.String(length), nullable=True)
        )
    op.create_check_constraint(
        "ck_agent_child_creation_audit",
        "agent_child_conversations",
        "(creation_policy_version IS NULL AND request_digest IS NULL "
        "AND expected_agent_digest IS NULL AND expected_persistence_dir IS NULL) OR "
        "(creation_policy_version IS NOT NULL AND creation_policy_version = 'binding-isolated-v1' "
        "AND request_digest IS NOT NULL "
        "AND request_digest ~ '^[0-9a-f]{64}$' AND expected_agent_digest IS NOT NULL "
        "AND expected_agent_digest ~ '^[0-9a-f]{64}$' AND expected_persistence_dir IS NOT NULL "
        "AND length(expected_persistence_dir) > 0)",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE agent_child_conversations IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_child_conversations "
        "WHERE creation_policy_version IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Cannot discard governed child creation audit'; END IF; END $$"
    )
    op.drop_constraint("ck_agent_child_creation_audit", "agent_child_conversations", type_="check")
    for name in (
        "creation_policy_version",
        "request_digest",
        "expected_agent_digest",
        "expected_persistence_dir",
    ):
        op.drop_column("agent_child_conversations", name)
