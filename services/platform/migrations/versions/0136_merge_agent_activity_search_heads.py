"""Merge Agent activity and partial-search migration heads.

Revision ID: 0136_merge_activity_search
Revises: 0117_agent_activity_projection, 0135_agent_search_partial
"""

from __future__ import annotations

revision = "0136_merge_activity_search"
down_revision = ("0117_agent_activity_projection", "0135_agent_search_partial")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join independent schema branches without changing persisted data."""


def downgrade() -> None:
    """The merge node has no schema operations of its own."""
