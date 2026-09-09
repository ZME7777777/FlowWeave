"""persist attributable OpenHands conversation token usage

Revision ID: 0105_conversation_token_usage
Revises: 0104_candidate_output_sets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0105_conversation_token_usage"
down_revision = "0104_candidate_output_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_usage_buckets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("flow_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("openhands_conversation_id", sa.String(length=100), nullable=False),
        sa.Column("usage_id", sa.String(length=200), nullable=False),
        sa.Column("usage_kind", sa.String(length=20), nullable=False, server_default="AUXILIARY"),
        sa.Column("model_name", sa.String(length=200), nullable=False, server_default="default"),
        sa.Column("baseline_cost_usd", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("observed_cost_usd", sa.Numeric(20, 8), nullable=False, server_default="0"),
        *(
            sa.Column(f"{phase}_{field}", sa.BigInteger(), nullable=False, server_default="0")
            for phase in ("baseline", "observed")
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            )
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "usage_id", name="uq_agent_conversation_usage_bucket"),
        sa.CheckConstraint(
            "baseline_prompt_tokens >= 0 AND baseline_completion_tokens >= 0 "
            "AND baseline_cache_read_tokens >= 0 AND baseline_cache_write_tokens >= 0 "
            "AND baseline_reasoning_tokens >= 0 AND baseline_cost_usd >= 0 "
            "AND observed_prompt_tokens >= baseline_prompt_tokens "
            "AND observed_completion_tokens >= baseline_completion_tokens "
            "AND observed_cache_read_tokens >= baseline_cache_read_tokens "
            "AND observed_cache_write_tokens >= baseline_cache_write_tokens "
            "AND observed_reasoning_tokens >= baseline_reasoning_tokens "
            "AND observed_cost_usd >= baseline_cost_usd",
            name="ck_agent_conversation_usage_monotonic",
        ),
    )
    for field in ("owner_user_id", "binding_id", "flow_run_id", "node_run_id", "node_attempt_id", "openhands_conversation_id"):
        op.create_index(f"ix_agent_conversation_usage_buckets_{field}", "agent_conversation_usage_buckets", [field])


def downgrade() -> None:
    op.drop_table("agent_conversation_usage_buckets")
