"""add runtime model and reasoning selections

Revision ID: 0022_attempt_model_reasoning
Revises: 0021_codex_oauth_model_providers
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_attempt_model_reasoning"
down_revision = "0021_codex_oauth_model_providers"
branch_labels = None
depends_on = None


def _add_missing(table: str, columns: tuple[sa.Column, ...]) -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing(
        "provider_models",
        (
            sa.Column("default_reasoning_effort", sa.String(30), nullable=True),
            sa.Column(
                "supported_reasoning_efforts",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        ),
    )
    _add_missing(
        "node_attempts",
        (
            sa.Column("model_name", sa.String(240), nullable=True),
            sa.Column("reasoning_effort", sa.String(30), nullable=True),
        ),
    )


def downgrade() -> None:
    for table, names in (
        ("node_attempts", ("reasoning_effort", "model_name")),
        (
            "provider_models",
            ("supported_reasoning_efforts", "default_reasoning_effort"),
        ),
    ):
        existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for name in names:
            if name in existing:
                op.drop_column(table, name)
