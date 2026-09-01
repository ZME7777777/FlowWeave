"""Compatibility handler for legacy Agent Conversation title tasks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from flowweave.modules.tasks.public import Lease
from flowweave.shared.models import BackgroundTask


def process_agent_conversation_title(
    db: Session, binding_id: str, payload: dict[str, Any], lease: Lease
) -> None:
    """Acknowledge a pre-native-autotitle task without changing its Conversation.

    New Agent Conversations use OpenHands' native ``autotitle``. Retaining
    this handler lets a Worker safely finish pre-upgrade tasks without any
    legacy provider request overwriting a native or manually chosen title.
    """

    del binding_id
    generation = payload.get("title_generation")
    db.execute(
        update(BackgroundTask)
        .where(
            BackgroundTask.id == lease.task_id,
            BackgroundTask.lease_owner == lease.owner,
            BackgroundTask.lease_generation == lease.generation,
        )
        .values(
            payload_json={
                "title_generation": generation if isinstance(generation, int) else 0
            }
        )
    )
