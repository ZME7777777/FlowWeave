from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DRAFT', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED')",
            name="ck_case_state",
        ),
        CheckConstraint("row_version >= 1", name="ck_case_row_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(220))
    description: Mapped[str] = mapped_column(Text, default="")
    materials_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    session_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    work_directory_id: Mapped[str | None] = mapped_column(String(36))
    objective: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    control_conversation_binding_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    control_work_directory_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    current_control_round_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CaseControlRound(Base):
    """Durable bootstrap identity and immutable context, not a conversation store."""

    __tablename__ = "case_control_rounds"
    __table_args__ = (
        UniqueConstraint("case_id", "idempotency_key", name="uq_case_control_round_key"),
        CheckConstraint(
            "state IN ('PENDING', 'STARTED', 'FAILED', 'DELETED')",
            name="ck_case_control_round_state",
        ),
        CheckConstraint("delivery_attempt >= 1", name="ck_case_control_round_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36))
    work_directory_id: Mapped[str] = mapped_column(String(36))
    binding_id: Mapped[str] = mapped_column(String(36), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(140))
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    delivery_attempt: Mapped[int] = mapped_column(Integer, default=1)
    session_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    context_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    initial_prompt: Mapped[str] = mapped_column(Text)
    message_cursor: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseToolGrant(Base):
    """Credential reference for one native coordinator, never a login session."""

    __tablename__ = "case_tool_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # Referential constraints live in the migration, like the other control-plane models.
    round_id: Mapped[str] = mapped_column(String(36), unique=True)
    binding_id: Mapped[str] = mapped_column(String(36), unique=True)
    work_directory_version_id: Mapped[str] = mapped_column(String(36))
    runtime_session_id: Mapped[str] = mapped_column(String(36))
    native_conversation_id: Mapped[str] = mapped_column(String(36))
    token_digest: Mapped[str] = mapped_column(String(64))
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary)
    capability_version_id: Mapped[str] = mapped_column(String(36))
    capability_digest: Mapped[str] = mapped_column(String(64))
    runtime_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseFlowToolGrant(Base):
    """Separately frozen workflow authority; context grants never authorize writes."""

    __tablename__ = "case_flow_tool_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # Referential constraints live in the migration, like the other control-plane models.
    round_id: Mapped[str] = mapped_column(String(36), unique=True)
    binding_id: Mapped[str] = mapped_column(String(36), unique=True)
    work_directory_version_id: Mapped[str] = mapped_column(String(36))
    runtime_session_id: Mapped[str] = mapped_column(String(36))
    native_conversation_id: Mapped[str] = mapped_column(String(36))
    token_digest: Mapped[str] = mapped_column(String(64))
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary)
    capability_version_id: Mapped[str] = mapped_column(String(36))
    capability_digest: Mapped[str] = mapped_column(String(64))
    runtime_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseMaterialToolGrant(Base):
    """Native read authority for registered outputs, distinct from workflow actions."""

    __tablename__ = "case_material_tool_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # Referential constraints live in the migration, like the other control-plane models.
    round_id: Mapped[str] = mapped_column(String(36), unique=True)
    binding_id: Mapped[str] = mapped_column(String(36), unique=True)
    work_directory_version_id: Mapped[str] = mapped_column(String(36))
    runtime_session_id: Mapped[str] = mapped_column(String(36))
    native_conversation_id: Mapped[str] = mapped_column(String(36))
    token_digest: Mapped[str] = mapped_column(String(64))
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary)
    capability_version_id: Mapped[str] = mapped_column(String(36))
    capability_digest: Mapped[str] = mapped_column(String(64))
    runtime_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseArtifactReference(Base):
    """Immutable provenance; content remains owned by the official ArtifactVersion."""

    __tablename__ = "case_artifact_references"
    __table_args__ = (
        UniqueConstraint("case_id", "artifact_version_id", name="uq_case_artifact_reference"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str] = mapped_column(String(36))
    stage_key: Mapped[str] = mapped_column(String(100))
    execution_flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str] = mapped_column(String(36))
    node_key: Mapped[str] = mapped_column(String(100))
    node_attempt_id: Mapped[str] = mapped_column(String(36))
    artifact_version_id: Mapped[str] = mapped_column(String(36), index=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseFlowRun(Base):
    __tablename__ = "case_flow_runs"
    __table_args__ = (UniqueConstraint("case_id", "flow_run_id", name="uq_case_flow_run"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseStage(Base):
    __tablename__ = "case_stages"
    __table_args__ = (
        UniqueConstraint("case_id", "stage_key", name="uq_case_stage_key"),
        UniqueConstraint("case_id", "position", name="uq_case_stage_position"),
        CheckConstraint(
            "state IN ('PENDING', 'ACTIVE', 'COMPLETED', 'SKIPPED')",
            name="ck_case_stage_state",
        ),
        CheckConstraint("position >= 0", name="ck_case_stage_position"),
        CheckConstraint(
            "execution_mode IN ('STEPWISE', 'CONTINUOUS')",
            name="ck_case_stage_execution_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(220))
    objective: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer)
    # The configured source record is a reusable Case launch template. It is
    # never started by this Case; each launch creates a separate execution copy.
    flow_run_id: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    current_execution_flow_run_id: Mapped[str | None] = mapped_column(
        String(36), unique=True, index=True
    )
    execution_mode: Mapped[str] = mapped_column(String(20), default="STEPWISE")
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CaseDecisionProposal(Base):
    __tablename__ = "case_decision_proposals"
    __table_args__ = (
        UniqueConstraint("case_id", "proposal_key", name="uq_case_decision_proposal_key"),
        CheckConstraint(
            "action IN ('START_STAGE_EXECUTION', 'CONTINUE_STEPWISE', 'STOP_FLOW_RUN', "
            "'RERUN_STEPWISE_NODE', 'REVISE_UNSTARTED_RUNTIME', 'COPY_AUTOMATIC_RUN')",
            name="ck_case_decision_proposal_action",
        ),
        CheckConstraint(
            "state IN ('PROPOSED', 'EXECUTED', 'FAILED', 'SUPERSEDED')",
            name="ck_case_decision_proposal_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    proposal_key: Mapped[str] = mapped_column(String(120))
    source_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    source_binding_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="PROPOSED", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseAgentTask(Base):
    __tablename__ = "case_agent_tasks"
    __table_args__ = (
        UniqueConstraint("case_id", "task_key", name="uq_case_agent_task_key"),
        CheckConstraint(
            "task_type IN ('OBSERVE', 'PROPOSE_REVISION', 'REVIEW')",
            name="ck_case_agent_task_type",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_case_agent_task_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str | None] = mapped_column(String(36), index=True)
    task_key: Mapped[str] = mapped_column(String(120))
    task_type: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    input_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CaseExecutionEvent(Base):
    """Transactional delivery inbox of source references, never a native event store."""

    __tablename__ = "case_execution_events"
    __table_args__ = (
        UniqueConstraint("case_id", "run_event_cursor", name="uq_case_execution_event_source"),
        CheckConstraint(
            "routing_state IN ('PENDING', 'HELD', 'AUDIT_ONLY')",
            name="ck_case_execution_event_routing",
        ),
        CheckConstraint("case_row_version >= 1", name="ck_case_execution_event_version"),
        Index("ix_case_execution_event_queue", "round_id", "routing_state", "run_event_cursor"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str] = mapped_column(String(36))
    stage_key: Mapped[str] = mapped_column(String(100))
    execution_flow_run_id: Mapped[str] = mapped_column(String(36))
    run_event_cursor: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(80))
    fact_type: Mapped[str] = mapped_column(String(40))
    node_run_id: Mapped[str | None] = mapped_column(String(36))
    node_key: Mapped[str | None] = mapped_column(String(100))
    node_attempt_id: Mapped[str | None] = mapped_column(String(36))
    candidate_output_set_id: Mapped[str | None] = mapped_column(String(36))
    artifact_version_id: Mapped[str | None] = mapped_column(String(36))
    native_completion_event_id: Mapped[str | None] = mapped_column(String(200))
    # Immutable routing/audit identities survive conversation or stage deletion.
    # Only the owning Case cascades this inbox; it does not retain source Runs.
    round_id: Mapped[str | None] = mapped_column(String(36))
    binding_id: Mapped[str | None] = mapped_column(String(36))
    case_row_version: Mapped[int] = mapped_column(Integer)
    case_state: Mapped[str] = mapped_column(String(20))
    stage_state: Mapped[str] = mapped_column(String(20))
    routing_state: Mapped[str] = mapped_column(String(20))
    routing_reason: Mapped[str | None] = mapped_column(String(80))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseFeedbackDelivery(Base):
    """Delivery audit references, never a copy of native messages or history."""

    __tablename__ = "case_feedback_deliveries"
    __table_args__ = (
        UniqueConstraint("case_id", "source_key", name="uq_case_feedback_source"),
        UniqueConstraint("execution_event_id", name="uq_case_feedback_execution_event"),
        CheckConstraint(
            "state IN ('PENDING', 'SUBMITTING', 'ACKNOWLEDGED', 'DELIVERED', "
            "'AMBIGUOUS', 'AUDIT_ONLY', 'FAILED')",
            name="ck_case_feedback_state",
        ),
        Index("ix_case_feedback_queue", "round_id", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    round_id: Mapped[str] = mapped_column(String(36))
    binding_id: Mapped[str] = mapped_column(String(36))
    source_key: Mapped[str] = mapped_column(String(140))
    source_kind: Mapped[str] = mapped_column(String(40))
    execution_event_id: Mapped[str | None] = mapped_column(String(36))
    case_row_version: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    message_event_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseControlPlan(Base):
    __tablename__ = "case_control_plans"
    __table_args__ = (
        UniqueConstraint("case_id", "idempotency_key", name="uq_case_control_plan_key"),
        CheckConstraint(
            "action IN ('CREATE_EXECUTION_COPY', 'START_EXECUTION', 'START_STEPWISE_NODE', "
            "'ACCEPT_STEPWISE_ATTEMPT', 'REJECT_STEPWISE_ATTEMPT', 'STOP_EXECUTION', "
            "'RERUN_STEPWISE_NODE', 'REVISE_UNSTARTED_RUNTIME', 'CONFIGURE_EXECUTION')",
            name="ck_case_control_plan_action",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'VALIDATED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_case_control_plan_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str] = mapped_column(String(36), index=True)
    source_binding_id: Mapped[str | None] = mapped_column(String(36), index=True)
    execution_flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(140))
    retry_of_plan_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    input_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = (
    "CaseFeedbackDelivery",
    "Case",
    "CaseAgentTask",
    "CaseControlPlan",
    "CaseExecutionEvent",
    "CaseControlRound",
    "CaseDecisionProposal",
    "CaseFlowRun",
    "CaseFlowToolGrant",
    "CaseMaterialToolGrant",
    "CaseArtifactReference",
    "CaseToolGrant",
    "CaseStage",
)
