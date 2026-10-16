"""allow the additive OH_FW_V2 FlowRun protocol profile.

Revision ID: 0149_add_oh_fw_v2_protocol_profile
Revises: 0148_environment_runtime_protocol_profile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0149_add_oh_fw_v2_protocol_profile"
down_revision = "0148_environment_runtime_protocol_profile"
branch_labels = None
depends_on = None

_V1_PROFILE = "OH_FW_V1"
_V2_PROFILE = "OH_FW_V2"

_CONSTRAINTS = (
    (
        "ck_environment_version_runtime_protocol_profile",
        "environment_versions",
        "runtime_protocol_profile",
    ),
    (
        "ck_flow_run_runtime_protocol_profile",
        "flow_run_runtimes",
        "protocol_profile",
    ),
    (
        "ck_runtime_generation_protocol_profile",
        "runtime_generations",
        "protocol_profile",
    ),
)


def _replace_allow_list(*, values: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{value}'" for value in values)
    for constraint, table, column in _CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} IN ({quoted})")


def upgrade() -> None:
    _replace_allow_list(values=(_V1_PROFILE, _V2_PROFILE))


def downgrade() -> None:
    bind = op.get_bind()
    for _constraint, table, column in _CONSTRAINTS:
        has_v2 = bind.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE {column} = :profile LIMIT 1"),
            {"profile": _V2_PROFILE},
        ).scalar()
        if has_v2:
            raise RuntimeError(f"Cannot downgrade while {table}.{column} contains {_V2_PROFILE}")
    _replace_allow_list(values=(_V1_PROFILE,))
