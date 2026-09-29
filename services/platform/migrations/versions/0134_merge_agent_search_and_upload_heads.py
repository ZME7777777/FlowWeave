"""Merge Agent search and resumable upload migration heads.

Revision ID: 0134_merge_search_upload
Revises: 0130_agent_conv_search_scope, 0133_workspace_resumable
"""

from __future__ import annotations

revision = "0134_merge_search_upload"
down_revision = (
    "0130_agent_conv_search_scope",
    "0133_workspace_resumable",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge independent revision branches without changing schema or data."""


def downgrade() -> None:
    """Split the independent revision branches without changing schema or data."""
