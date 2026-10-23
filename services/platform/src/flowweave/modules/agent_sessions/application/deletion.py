"""Application-owned physical deletion for Agent session records."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.resumable_attachments import (
    delete_attachment_uploads,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationCredentialSync,
    AgentConversationHistoryGrant,
    AgentConversationMessageAttachment,
    AgentConversationSchedule,
    AgentConversationScheduleOccurrence,
)
from flowweave.shared.models import BackgroundTask, RuntimeConfirmationApproval


def delete_binding_records(db: Session, binding_id: str) -> None:
    """Physically remove one binding and every record it owns.

    Callers remain responsible for deleting the OpenHands Conversation first.
    Deletion command rows are part of the binding graph and disappear with it;
    independent audit facts live outside the Conversation locator tables.
    """

    binding = db.get(AgentConversationBinding, binding_id)
    if binding is not None:
        delete_attachment_uploads(
            db,
            binding_id=binding_id,
            host_kind=binding.host_kind,
            host_id=binding.host_id,
            host_scope_id=binding.node_attempt_id,
        )
    db.execute(
        delete(RuntimeConfirmationApproval).where(
            RuntimeConfirmationApproval.flow_run_conversation_binding_id == binding_id
        )
    )
    db.execute(delete(BackgroundTask).where(BackgroundTask.aggregate_id == binding_id))
    db.execute(
        delete(AgentConversationHistoryGrant).where(
            (AgentConversationHistoryGrant.target_binding_id == binding_id)
            | (AgentConversationHistoryGrant.source_binding_id == binding_id)
        )
    )
    schedule_ids = select(AgentConversationSchedule.id).where(
        AgentConversationSchedule.binding_id == binding_id
    )
    db.execute(
        delete(BackgroundTask).where(
            BackgroundTask.aggregate_type == "AGENT_CONVERSATION_SCHEDULE_OCCURRENCE",
            BackgroundTask.aggregate_id.in_(
                select(AgentConversationScheduleOccurrence.id).where(
                    AgentConversationScheduleOccurrence.schedule_id.in_(schedule_ids)
                )
            ),
        )
    )
    db.execute(
        delete(AgentConversationScheduleOccurrence).where(
            AgentConversationScheduleOccurrence.schedule_id.in_(schedule_ids)
        )
    )
    db.execute(
        delete(AgentConversationSchedule).where(AgentConversationSchedule.binding_id == binding_id)
    )
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
        delete(AgentConversationCredentialSync).where(
            AgentConversationCredentialSync.binding_id == binding_id
        )
    )
    db.execute(
        delete(AgentConversationCommand).where(AgentConversationCommand.binding_id == binding_id)
    )
    db.execute(delete(AgentConversationBinding).where(AgentConversationBinding.id == binding_id))


__all__ = ("delete_binding_records",)
