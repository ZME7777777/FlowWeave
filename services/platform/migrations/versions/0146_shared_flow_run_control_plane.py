"""share top-level FlowRun control-plane resources.

Revision ID: 0146_shared_flow_runs
Revises: 0145_tenant_flow_run_numbers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0146_shared_flow_runs"
down_revision = "0145_tenant_flow_run_numbers"
branch_labels = None
depends_on = None

_FLOWWEAVE_USER_ID = "6311561c-06e4-41ad-8afe-aac35cfa83ec"
_SPECIAL_TABLES = (
    "flow_runs",
    "run_snapshots",
    "flow_run_runtime_secret_references",
    "flow_run_runtime_allocations",
    "flow_run_runtimes",
    "runtime_generations",
)


def _replace_policy(table: str, predicate: str) -> None:
    policy = f"pl_tenant_{table}"
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'CREATE POLICY "{policy}" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
        )
    )


def _private_predicate() -> str:
    return (
        "current_setting('flowweave.bypass', true) = 'on' OR "
        "owner_user_id = current_setting('flowweave.user_id', true)"
    )


def upgrade() -> None:
    op.drop_constraint("uq_flow_run_number", "flow_runs", type_="unique")
    op.drop_constraint("uq_run_node_sequence", "node_runs", type_="unique")
    op.create_unique_constraint(
        "uq_run_node_sequence",
        "node_runs",
        ["owner_user_id", "flow_run_id", "sequence_no"],
    )
    op.drop_constraint("uq_artifact_field_version", "artifact_versions", type_="unique")
    op.create_unique_constraint(
        "uq_artifact_field_version",
        "artifact_versions",
        ["owner_user_id", "flow_run_id", "field_key", "version_no"],
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       flow_definition_id,
                       run_no,
                       row_number() OVER (
                           PARTITION BY flow_definition_id, run_no
                           ORDER BY started_at, id
                       ) AS duplicate_rank
                FROM flow_runs
                WHERE parent_flow_run_id IS NULL
            ),
            maxima AS (
                SELECT flow_definition_id, max(run_no) AS max_run_no
                FROM flow_runs
                WHERE parent_flow_run_id IS NULL
                GROUP BY flow_definition_id
            ),
            replacements AS (
                SELECT ranked.id,
                       maxima.max_run_no + row_number() OVER (
                           PARTITION BY ranked.flow_definition_id
                           ORDER BY ranked.run_no, ranked.id
                       ) AS new_run_no
                FROM ranked
                JOIN maxima USING (flow_definition_id)
                WHERE ranked.duplicate_rank > 1
            )
            UPDATE flow_runs AS target
            SET run_no = replacements.new_run_no
            FROM replacements
            WHERE target.id = replacements.id
            """
        )
    )
    op.execute(
        sa.text(
            "UPDATE flow_runs SET owner_user_id = :owner WHERE parent_flow_run_id IS NULL"
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    op.execute(
        sa.text(
            "UPDATE run_snapshots AS snapshot SET owner_user_id = :owner "
            "FROM flow_runs AS run "
            "WHERE snapshot.flow_run_id = run.id AND run.parent_flow_run_id IS NULL"
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    for table in ("flow_run_runtime_allocations", "flow_run_runtimes"):
        op.execute(
            sa.text(
                f'UPDATE "{table}" SET owner_user_id = :owner WHERE node_attempt_id IS NULL'
            ).bindparams(owner=_FLOWWEAVE_USER_ID)
        )
    op.execute(
        sa.text(
            "UPDATE runtime_generations AS generation SET owner_user_id = :owner "
            "FROM flow_run_runtimes AS runtime "
            "WHERE generation.runtime_session_id = runtime.id "
            "AND runtime.node_attempt_id IS NULL"
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    op.execute(
        sa.text(
            "UPDATE flow_run_runtime_secret_references AS secret "
            "SET owner_user_id = :owner "
            "FROM flow_run_runtime_allocations AS allocation "
            "WHERE secret.id = allocation.secret_reference_id "
            "AND allocation.node_attempt_id IS NULL"
        ).bindparams(owner=_FLOWWEAVE_USER_ID)
    )
    op.create_index(
        "uq_flow_run_number",
        "flow_runs",
        ["flow_definition_id", "run_no"],
        unique=True,
        postgresql_where=sa.text("parent_flow_run_id IS NULL"),
    )
    op.create_index(
        "uq_flow_run_record_number",
        "flow_runs",
        ["owner_user_id", "flow_definition_id", "run_no"],
        unique=True,
        postgresql_where=sa.text("parent_flow_run_id IS NOT NULL"),
    )

    bypass = "current_setting('flowweave.bypass', true) = 'on'"
    current = "owner_user_id = current_setting('flowweave.user_id', true)"
    shared_owner = f"owner_user_id = '{_FLOWWEAVE_USER_ID}'"
    _replace_policy(
        "flow_runs",
        f"{bypass} OR {current} OR ({shared_owner} AND parent_flow_run_id IS NULL)",
    )
    _replace_policy(
        "run_snapshots",
        f"{bypass} OR {current} OR ({shared_owner} AND EXISTS ("
        "SELECT 1 FROM flow_runs AS shared_run "
        "WHERE shared_run.id = run_snapshots.flow_run_id "
        "AND shared_run.parent_flow_run_id IS NULL))",
    )
    for table in ("flow_run_runtime_allocations", "flow_run_runtimes"):
        _replace_policy(
            table,
            f"{bypass} OR {current} OR ({shared_owner} AND node_attempt_id IS NULL)",
        )
    _replace_policy(
        "runtime_generations",
        f"{bypass} OR {current} OR ({shared_owner} AND EXISTS ("
        "SELECT 1 FROM flow_run_runtimes AS shared_runtime "
        "WHERE shared_runtime.id = runtime_generations.runtime_session_id "
        "AND shared_runtime.node_attempt_id IS NULL))",
    )
    _replace_policy(
        "flow_run_runtime_secret_references",
        f"{bypass} OR {current} OR ({shared_owner} AND EXISTS ("
        "SELECT 1 FROM flow_run_runtime_allocations AS shared_allocation "
        "WHERE shared_allocation.secret_reference_id = "
        "flow_run_runtime_secret_references.id "
        "AND shared_allocation.node_attempt_id IS NULL))",
    )


def downgrade() -> None:
    for table in _SPECIAL_TABLES:
        _replace_policy(table, _private_predicate())
    op.drop_constraint("uq_artifact_field_version", "artifact_versions", type_="unique")
    op.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY flow_run_id, field_key
                           ORDER BY version_no, created_at, id
                       ) AS new_version_no
                FROM artifact_versions
            )
            UPDATE artifact_versions AS target
            SET version_no = numbered.new_version_no
            FROM numbered
            WHERE target.id = numbered.id
              AND target.version_no <> numbered.new_version_no
            """
        )
    )
    op.create_unique_constraint(
        "uq_artifact_field_version",
        "artifact_versions",
        ["flow_run_id", "field_key", "version_no"],
    )
    op.drop_constraint("uq_run_node_sequence", "node_runs", type_="unique")
    op.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY flow_run_id
                           ORDER BY sequence_no, activated_at, id
                       ) AS new_sequence_no
                FROM node_runs
            )
            UPDATE node_runs AS target
            SET sequence_no = numbered.new_sequence_no
            FROM numbered
            WHERE target.id = numbered.id
              AND target.sequence_no <> numbered.new_sequence_no
            """
        )
    )
    op.create_unique_constraint(
        "uq_run_node_sequence",
        "node_runs",
        ["flow_run_id", "sequence_no"],
    )
    op.drop_index("uq_flow_run_record_number", table_name="flow_runs")
    op.drop_index("uq_flow_run_number", table_name="flow_runs")
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       owner_user_id,
                       flow_definition_id,
                       run_no,
                       row_number() OVER (
                           PARTITION BY owner_user_id, flow_definition_id, run_no
                           ORDER BY (parent_flow_run_id IS NULL) DESC, started_at, id
                       ) AS duplicate_rank
                FROM flow_runs
            ),
            maxima AS (
                SELECT owner_user_id, flow_definition_id, max(run_no) AS max_run_no
                FROM flow_runs
                GROUP BY owner_user_id, flow_definition_id
            ),
            replacements AS (
                SELECT ranked.id,
                       maxima.max_run_no + row_number() OVER (
                           PARTITION BY ranked.owner_user_id, ranked.flow_definition_id
                           ORDER BY ranked.run_no, ranked.id
                       ) AS new_run_no
                FROM ranked
                JOIN maxima USING (owner_user_id, flow_definition_id)
                WHERE ranked.duplicate_rank > 1
            )
            UPDATE flow_runs AS target
            SET run_no = replacements.new_run_no
            FROM replacements
            WHERE target.id = replacements.id
            """
        )
    )
    op.create_unique_constraint(
        "uq_flow_run_number",
        "flow_runs",
        ["owner_user_id", "flow_definition_id", "run_no"],
    )
