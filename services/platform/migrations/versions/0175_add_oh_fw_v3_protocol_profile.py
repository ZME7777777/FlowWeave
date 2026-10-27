"""Allow additive V3 profiles without upgrading any persisted Runtime.

Revision ID: 0175_add_oh_fw_v3_protocol_profile
Revises: 0174_agent_workspace_user_defaults
"""

from alembic import op

revision = "0175_add_oh_fw_v3_protocol_profile"
down_revision = "0174_agent_workspace_user_defaults"
branch_labels = None
depends_on = None

_CONSTRAINTS = (
    (
        "ck_environment_version_runtime_protocol_profile",
        "environment_versions",
        "runtime_protocol_profile",
    ),
    ("ck_flow_run_runtime_protocol_profile", "flow_run_runtimes", "protocol_profile"),
    ("ck_runtime_generation_protocol_profile", "runtime_generations", "protocol_profile"),
)


def _replace_allow_list(values: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{value}'" for value in values)
    for constraint, table, column in _CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} IN ({quoted})")


def upgrade() -> None:
    _replace_allow_list(("OH_FW_V1", "OH_FW_V2", "OH_FW_V3"))


def downgrade() -> None:
    # Hold the same locks through the guards and DDL; new V3 writes cannot race
    # a successful check. Refuse rollback rather than rewriting frozen rows.
    op.execute(
        "LOCK TABLE environment_versions, flow_run_runtimes, runtime_generations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security = off")
    for _constraint, table, column in _CONSTRAINTS:
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} WHERE {column} = 'OH_FW_V3') "
            f"THEN RAISE EXCEPTION 'Cannot downgrade while {table} contains OH_FW_V3'; "
            "END IF; END $$"
        )
    _replace_allow_list(("OH_FW_V1", "OH_FW_V2"))
