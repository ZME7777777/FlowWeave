"""Freeze additive V4 child governance without changing existing profiles.

Revision ID: 0178_add_oh_fw_v4_protocol_profile
Revises: 0177_agent_child_parent_manifest
"""

from alembic import op

revision = "0178_add_oh_fw_v4_protocol_profile"
down_revision = "0177_agent_child_parent_manifest"
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
    _replace_allow_list(("OH_FW_V1", "OH_FW_V2", "OH_FW_V3", "OH_FW_V4"))


def downgrade() -> None:
    op.execute(
        "LOCK TABLE environment_versions, flow_run_runtimes, runtime_generations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security = off")
    for _constraint, table, column in _CONSTRAINTS:
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} WHERE {column} = 'OH_FW_V4') "
            f"THEN RAISE EXCEPTION 'Cannot downgrade while {table} contains OH_FW_V4'; "
            "END IF; END $$"
        )
    _replace_allow_list(("OH_FW_V1", "OH_FW_V2", "OH_FW_V3"))
