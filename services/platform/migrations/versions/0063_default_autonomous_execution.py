"""default new executions to no confirmation and automatic condensation

Revision ID: 0063_autonomous_defaults
Revises: 0062_agent_conversation_provider
Create Date: 2026-08-26
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0063_autonomous_defaults"
down_revision = "0062_agent_conversation_provider"
branch_labels = None
depends_on = None

_SUMMARIZING = {
    "kind": "LLM_SUMMARIZING",
    "model_provider_id": None,
    "model_name": None,
    "max_size": 240,
    "max_tokens": None,
    "keep_first": 2,
    "minimum_progress": 0.1,
    "hard_context_reset_max_retries": 5,
    "hard_context_reset_context_scaling": 0.8,
}


def upgrade() -> None:
    # Node assets are mutable authoring state. Updating them changes future Run
    # Snapshots only; existing immutable Snapshots and Attempts stay untouched.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE node_executor_configs "
            "SET confirmation_policy = 'NEVER', "
            "condenser_config_json = CAST(:condenser AS JSON)"
        ),
        {"condenser": json.dumps(_SUMMARIZING)},
    )


def downgrade() -> None:
    # A downgrade restores the former product defaults for mutable node assets;
    # immutable execution history is intentionally never rewritten.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE node_executor_configs "
            "SET confirmation_policy = 'ALWAYS', "
            "condenser_config_json = CAST(:condenser AS JSON)"
        ),
        {"condenser": json.dumps({"kind": "NO_OP"})},
    )
