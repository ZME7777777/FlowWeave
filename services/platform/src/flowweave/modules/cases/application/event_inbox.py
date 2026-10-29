"""Capture important execution facts in the producer transaction.

The inbox owns routing/audit metadata and references only. RunEvent, formal
artifacts and native OpenHands events retain their existing ownership. A future
consumer must revalidate current scope before sending to the frozen recipient.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlRound,
    CaseExecutionEvent,
    CaseStage,
)
from flowweave.modules.runs.infrastructure.models import (
    ArtifactVersion,
    CandidateOutputSet,
    NodeAttempt,
    NodeRun,
    RunEvent,
)
from flowweave.shared.database import now, uid

# Explicit business facts only. Tool/configuration success, token deltas,
# heartbeats and scheduler polling must not create self-triggering wakeups.
EVENT_FACTS = {
    "ARTIFACT_VERSION_CREATED": "ARTIFACT_REGISTERED",
    "RUNTIME_COMPLETION_PROJECTED": "NODE_EXECUTION_FINISHED",
    "NODE_RUN_COMPLETED": "NODE_ACCEPTED",
    "AUTOMATIC_NODE_ACCEPTED": "NODE_ACCEPTED",
    "ATTEMPT_REJECTED": "NODE_REJECTED",
    "RUNTIME_FAILED": "EXECUTION_FAILED",
    "RUNTIME_DELIVERY_FAILED": "EXECUTION_FAILED",
    "RUNTIME_COMPLETION_IDENTITY_MISSING": "OUTPUT_BLOCKED",
    "RUNTIME_OUTPUT_VERSION_GROWTH_BLOCKED": "OUTPUT_BLOCKED",
    "AUTOMATIC_SCHEDULER_FAILED": "EXECUTION_FAILED",
    "ATTEMPT_CANCEL_FAILED": "EXECUTION_FAILED",
    "ATTEMPT_CANCELLED": "EXECUTION_CANCELLED",
    "FLOW_RUN_COMPLETED": "FLOW_EXECUTION_FINISHED",
    "FLOW_RUN_CANCELLED": "FLOW_EXECUTION_CANCELLED",
    "ATTEMPT_WAITING_INPUT": "INPUT_BLOCKED",
    "GATE_STAGE_FINISHED": "GATE_EVALUATED",
    "AUTOMATIC_GATE_REVIEW_REQUIRED": "REVIEW_REQUIRED",
    "AUTOMATIC_GATE_EXECUTION_FAILED": "EXECUTION_FAILED",
    "HUMAN_CONFIRM_REQUIRED": "CONFIRMATION_REQUIRED",
    "RUNTIME_CONFIRMATION_REQUIRED": "CONFIRMATION_REQUIRED",
    "RUNTIME_HUMAN_INPUT_REQUIRED": "INPUT_BLOCKED",
    "RUNTIME_CONFIRMATION_DRIFTED": "CONFIRMATION_REQUIRED",
}


def record_case_execution_event(db: Session, event: RunEvent) -> None:
    """Append once per formal source cursor, with no commit or external I/O.

    The producer passes the flushed RunEvent itself, never a model supplied
    identity. Only the current execution is eligible; a mere CaseFlowRun link
    or a template grants no routing authority. Capture before legacy lifecycle
    reconciliation so terminal facts retain their original coordinator target.
    """
    fact_type = EVENT_FACTS.get(event.event_type)
    if fact_type is None:
        return
    recipients = db.execute(
        select(
            Case.id.label("case_id"),
            Case.owner_user_id,
            Case.state.label("case_state"),
            Case.row_version,
            Case.control_conversation_binding_id.label("current_binding_id"),
            CaseStage.id.label("stage_id"),
            CaseStage.stage_key,
            CaseStage.state.label("stage_state"),
            CaseControlRound.id.label("round_id"),
            CaseControlRound.binding_id,
            CaseControlRound.state.label("round_state"),
            CaseControlRound.created_at.label("round_created_at"),
        )
        .join(CaseStage, CaseStage.case_id == Case.id)
        .outerjoin(
            CaseControlRound,
            (CaseControlRound.id == Case.current_control_round_id)
            & (CaseControlRound.case_id == Case.id)
            & (CaseControlRound.owner_user_id == Case.owner_user_id),
        )
        .where(
            CaseStage.current_execution_flow_run_id == event.flow_run_id,
            CaseStage.owner_user_id == Case.owner_user_id,
            Case.owner_user_id == event.owner_user_id,
        )
    ).all()
    if not recipients:
        return

    attempt = db.get(NodeAttempt, event.attempt_id) if event.attempt_id else None
    node_id = event.node_run_id or (attempt.node_run_id if attempt else None)
    node = db.get(NodeRun, node_id) if node_id else None
    if (event.attempt_id and (attempt is None or attempt.node_run_id != node_id)) or (
        node_id and (node is None or node.flow_run_id != event.flow_run_id)
    ):
        # Malformed/cross-tenant lineage never acquires a Case delivery target.
        return
    artifact = None
    if event.event_type == "ARTIFACT_VERSION_CREATED":
        artifact_id = event.payload_json.get("artifact_id")
        artifact = db.get(ArtifactVersion, artifact_id) if isinstance(artifact_id, str) else None
        if (
            artifact is None
            or attempt is None
            or artifact.producer_attempt_id != attempt.id
            or artifact.flow_run_id != event.flow_run_id
        ):
            # Ordinary inputs and unregistered conversational files are not outputs.
            return
    candidate = (
        db.get(CandidateOutputSet, attempt.current_candidate_output_set_id)
        if attempt and attempt.current_candidate_output_set_id
        else None
    )
    if candidate is not None and (attempt is None or candidate.attempt_id != attempt.id):
        return
    # A current-candidate pointer is useful context, but it is not a causal
    # event id for an unrelated failure/acceptance. Only copy native identity
    # explicitly supplied by the formal producer and validated output lineage.
    completion_id = artifact.runtime_completion_event_id if artifact else None
    if event.event_type == "RUNTIME_COMPLETION_PROJECTED" and candidate is not None:
        if event.payload_json.get("completion_event_id") == candidate.completion_event_id:
            completion_id = candidate.completion_event_id
    for recipient in recipients:
        routing, reason = "PENDING", None
        if recipient.case_state in {"COMPLETED", "ARCHIVED"}:
            routing, reason = "AUDIT_ONLY", "CASE_TERMINAL"
        elif (
            recipient.round_state != "STARTED"
            or recipient.binding_id != recipient.current_binding_id
        ):
            routing, reason = "AUDIT_ONLY", "COORDINATOR_UNAVAILABLE"
        elif recipient.round_created_at > event.occurred_at:
            routing, reason = "AUDIT_ONLY", "COORDINATOR_NEWER_THAN_SOURCE"
        elif recipient.stage_state != "ACTIVE":
            routing, reason = "AUDIT_ONLY", "STAGE_NOT_ACTIVE"
        elif recipient.case_state == "PAUSED":
            routing, reason = "HELD", "CASE_PAUSED"
        elif recipient.case_state != "ACTIVE":
            routing, reason = "AUDIT_ONLY", "CASE_NOT_ACTIVE"
        db.execute(
            insert(CaseExecutionEvent)
            .values(
                id=uid(),
                owner_user_id=recipient.owner_user_id,
                case_id=recipient.case_id,
                stage_id=recipient.stage_id,
                stage_key=recipient.stage_key,
                execution_flow_run_id=event.flow_run_id,
                run_event_cursor=event.cursor,
                event_type=event.event_type,
                fact_type=fact_type,
                node_run_id=node.id if node else None,
                node_key=node.flow_node_snapshot_key if node else None,
                node_attempt_id=attempt.id if attempt else None,
                candidate_output_set_id=candidate.id if candidate else None,
                artifact_version_id=artifact.id if artifact else None,
                native_completion_event_id=completion_id,
                round_id=recipient.round_id,
                binding_id=recipient.binding_id,
                case_row_version=recipient.row_version,
                case_state=recipient.case_state,
                stage_state=recipient.stage_state,
                routing_state=routing,
                routing_reason=reason,
                occurred_at=event.occurred_at,
                created_at=now(),
            )
            .on_conflict_do_nothing(constraint="uq_case_execution_event_source")
        )
