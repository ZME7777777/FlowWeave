"""Registered Case outputs and native material read authority."""

import sqlalchemy as sa
from alembic import op

revision = "0185_case_materials"
down_revision = "0184_cascade_directory_archives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_material_tool_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "round_id",
            sa.String(36),
            sa.ForeignKey("case_control_rounds.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "binding_id",
            sa.String(36),
            sa.ForeignKey("agent_conversation_bindings.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("work_directory_version_id", sa.String(36), nullable=False),
        sa.Column("runtime_session_id", sa.String(36), nullable=False),
        sa.Column("native_conversation_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("capability_version_id", sa.String(36), nullable=False),
        sa.Column("capability_digest", sa.String(64), nullable=False),
        sa.Column("runtime_manifest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_case_material_tool_grants_owner_user_id", "case_material_tool_grants", ["owner_user_id"]
    )
    predicate = (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )
    op.execute('ALTER TABLE "case_material_tool_grants" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_material_tool_grants" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_material_tool_grants" ON "case_material_tool_grants" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.create_table(
        "case_artifact_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column(
            "case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("stage_id", sa.String(36), nullable=False),
        sa.Column("stage_key", sa.String(100), nullable=False),
        sa.Column(
            "execution_flow_run_id",
            sa.String(36),
            sa.ForeignKey("flow_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("node_run_id", sa.String(36), nullable=False),
        sa.Column("node_key", sa.String(100), nullable=False),
        sa.Column("node_attempt_id", sa.String(36), nullable=False),
        sa.Column(
            "artifact_version_id",
            sa.String(36),
            sa.ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "artifact_version_id", name="uq_case_artifact_reference"),
    )
    for column in ("owner_user_id", "case_id", "execution_flow_run_id", "artifact_version_id"):
        op.create_index(
            "ix_case_artifact_references_" + column, "case_artifact_references", [column]
        )
    op.execute('ALTER TABLE "case_artifact_references" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "case_artifact_references" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "pl_tenant_case_artifact_references" ON "case_artifact_references" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )

    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('flowweave.bypass', true)"))
    connection.execute(sa.text("SELECT set_config('flowweave.bypass', 'on', true)"))
    op.execute("""
        INSERT INTO case_artifact_references
            (id, owner_user_id, case_id, stage_id, stage_key, execution_flow_run_id,
             node_run_id, node_key, node_attempt_id, artifact_version_id, registered_at)
        SELECT gen_random_uuid()::text, s.owner_user_id, s.case_id, s.id, s.stage_key,
               a.flow_run_id, n.id, n.flow_node_snapshot_key, t.id, a.id, a.created_at
        FROM case_stages s
        JOIN artifact_versions a ON a.flow_run_id = s.current_execution_flow_run_id
                                AND a.owner_user_id = s.owner_user_id
        JOIN node_attempts t ON t.id = a.producer_attempt_id AND t.owner_user_id = s.owner_user_id
        JOIN node_runs n ON n.id = t.node_run_id AND n.flow_run_id = a.flow_run_id
                        AND n.owner_user_id = s.owner_user_id
        ON CONFLICT (case_id, artifact_version_id) DO NOTHING
    """)
    connection.execute(
        sa.text("SELECT set_config('flowweave.bypass', :value, true)"), {"value": previous or ""}
    )


def downgrade() -> None:
    op.drop_table("case_artifact_references")
    op.drop_table("case_material_tool_grants")
