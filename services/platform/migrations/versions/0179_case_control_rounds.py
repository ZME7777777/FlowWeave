"""Persist Case coordination rounds and bootstrap snapshots."""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0179_case_control_rounds"
down_revision = "0178_merge_schedule_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("current_control_round_id", sa.String(36), nullable=True))
    op.create_unique_constraint(
        "uq_cases_current_control_round_id", "cases", ["current_control_round_id"]
    )
    table = op.create_table(
        "case_control_rounds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False, index=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("work_directory_id", sa.String(36), nullable=False),
        sa.Column("binding_id", sa.String(36), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(140), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("delivery_attempt", sa.Integer(), nullable=False),
        sa.Column("session_config_json", sa.JSON(), nullable=False),
        sa.Column("context_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("initial_prompt", sa.Text(), nullable=False),
        sa.Column("message_cursor", sa.String(200), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("case_id", "idempotency_key", name="uq_case_control_round_key"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'STARTED', 'FAILED', 'DELETED')",
            name="ck_case_control_round_state",
        ),
        sa.CheckConstraint("delivery_attempt >= 1", name="ck_case_control_round_attempt"),
    )
    op.create_index(
        "ix_case_control_rounds_owner_user_id", "case_control_rounds", ["owner_user_id"]
    )
    # Retain the existing native locator. Its original configuration/context was
    # not snapshotted, so explicitly mark it legacy instead of inventing history.
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT c.id, c.owner_user_id, c.control_conversation_binding_id AS binding_id, "
                "c.control_work_directory_id AS directory_id, b.workspace_id, b.created_at "
                "FROM cases c JOIN agent_conversation_bindings b "
                "ON b.id = c.control_conversation_binding_id AND b.owner_user_id = c.owner_user_id "
                "WHERE c.control_work_directory_id IS NOT NULL AND b.workspace_id IS NOT NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        round_id = str(uuid4())
        connection.execute(
            table.insert().values(
                id=round_id,
                owner_user_id=row["owner_user_id"],
                case_id=row["id"],
                workspace_id=row["workspace_id"],
                work_directory_id=row["directory_id"],
                binding_id=row["binding_id"],
                idempotency_key=f"legacy:{row['binding_id']}",
                state="STARTED",
                delivery_attempt=1,
                session_config_json={},
                context_snapshot_json={"legacy": True},
                initial_prompt="",
                created_at=row["created_at"],
                started_at=row["created_at"],
            )
        )
        connection.execute(
            sa.text("UPDATE cases SET current_control_round_id = :round WHERE id = :case"),
            {"round": round_id, "case": row["id"]},
        )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "case_control_rounds" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_control_rounds" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_control_rounds" ON "case_control_rounds" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.drop_table("case_control_rounds")
    op.drop_constraint("uq_cases_current_control_round_id", "cases", type_="unique")
    op.drop_column("cases", "current_control_round_id")
