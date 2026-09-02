"""Application-owned physical deletion for Agent session records."""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)
from flowweave.shared.models import BackgroundTask, RuntimeConfirmationApproval


def delete_binding_records(db: Session, binding_id: str) -> None:
    """Physically remove one binding and every record it owns.

    Callers remain responsible for deleting the OpenHands Conversation first.
    Deletion command rows are part of the binding graph and disappear with it;
    independent audit facts live outside the Conversation locator tables.
    """

    db.execute(
        delete(RuntimeConfirmationApproval).where(
            RuntimeConfirmationApproval.flow_run_conversation_binding_id == binding_id
        )
    )
    db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id == binding_id))
    db.execute(
        delete(AgentConversationMessageAttachment).where(
            AgentConversationMessageAttachment.binding_id == binding_id
        )
    )
    db.execute(
        delete(AgentConversationCapability).where(
            AgentConversationCapability.binding_id == binding_id
        )
    )
    db.execute(
        delete(AgentConversationCommand).where(AgentConversationCommand.binding_id == binding_id)
    )
    db.execute(delete(AgentConversationBinding).where(AgentConversationBinding.id == binding_id))


__all__ = ("delete_binding_records",)
