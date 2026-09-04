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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid
from flowweave.shared.domain.enums import AttemptState, FlowRunState, NodeRunState


class FlowRun(Base):
    __tablename__ = "flow_runs"
    __table_args__ = (
        CheckConstraint("run_mode IN ('MANUAL', 'AUTOMATIC')", name="ck_flow_runs_run_mode"),
        UniqueConstraint("flow_definition_id", "run_no", name="uq_flow_run_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_definition_id: Mapped[str] = mapped_column(String(36), index=True)
    run_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(220))
    # MANUAL preserves the current node-by-node workbench. AUTOMATIC records
    # begin as editable plans and only acquire Runtime/NodeRun state when a
    # later explicit start command freezes them.
    run_mode: Mapped[str] = mapped_column(String(20), default="MANUAL", index=True)
    automation_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Automatic runs are records inside one user-created FlowRun.  Keeping the
    # execution as a FlowRun lets the mature automatic scheduler remain
    # unchanged while this self reference supplies the product-level owner.
    parent_flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # A FlowRun remains one concrete execution.  These optional locators only
    # record that it was created by the independent schedule directory.
    schedule_id: Mapped[str | None] = mapped_column(String(36), index=True)
    schedule_occurrence_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(30), default=FlowRunState.ACTIVE)
    active_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    # The database foreign key is added by migration 0015. Keeping the ORM
    # mapping free of this cross-module FK lets historical migration 0003
    # create flow_runs before migration 0014 creates environment_versions.
    environment_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    completion_mode: Mapped[str | None] = mapped_column(String(10))
    # Created lazily when a node is actually executed and needs Lark outputs.
    lark_folder_token: Mapped[str | None] = mapped_column(String(200))
    lark_folder_url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FlowRunSchedule(Base):
    """A user-managed schedule directory; never an execution itself."""

    __tablename__ = "flow_run_schedules"
    __table_args__ = (
        CheckConstraint("run_mode IN ('MANUAL', 'AUTOMATIC')", name="ck_schedule_run_mode"),
        CheckConstraint("interval_minutes >= 1", name="ck_schedule_interval_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_definition_id: Mapped[str] = mapped_column(String(36), index=True)
    environment_version_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(220))
    run_mode: Mapped[str] = mapped_column(String(20), index=True)
    start_node_key: Mapped[str] = mapped_column(String(100))
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # A future configuration-update command increments this generation. Each
    # occurrence records the generation that supplied its frozen plan.
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_execution: Mapped[bool] = mapped_column(default=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class FlowRunScheduleOccurrence(Base):
    __tablename__ = "flow_run_schedule_occurrences"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_occurrence_slot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    schedule_id: Mapped[str] = mapped_column(String(36), index=True)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_kind: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RunSnapshot(Base):
    __tablename__ = "run_snapshots"
    __table_args__ = (UniqueConstraint("flow_run_id", "version", name="uq_snapshot_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    runtime_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    runtime_manifest_hash: Mapped[str] = mapped_column(String(64))
    # Nullable only for pre-FR-01 history. New snapshots freeze the same
    # Environment Version as their owning FlowRun.
    environment_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_by_action_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NodeRun(Base):
    __tablename__ = "node_runs"
    __table_args__ = (UniqueConstraint("flow_run_id", "sequence_no", name="uq_run_node_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    flow_node_snapshot_key: Mapped[str] = mapped_column(String(100))
    sequence_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(220))
    state: Mapped[str] = mapped_column(String(20), default=NodeRunState.ACTIVE)
    accepted_attempt_id: Mapped[str | None] = mapped_column(String(36))
    created_from: Mapped[str] = mapped_column(String(30), default="HUMAN_START")
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NodeAttempt(Base):
    __tablename__ = "node_attempts"
    __table_args__ = (UniqueConstraint("node_run_id", "attempt_no", name="uq_node_attempt_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    node_run_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[str] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(40), default=AttemptState.WAITING_INPUT)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    runtime_phase: Mapped[str | None] = mapped_column(String(30))
    # OpenHands-native locator reference only. An Attempt does not own the
    # Conversation and this column deliberately has no platform Conversation FK.
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    workspace_ref: Mapped[str | None] = mapped_column(Text)
    startup_mode: Mapped[str] = mapped_column(String(20), default="PROMPT")
    startup_capability_key: Mapped[str | None] = mapped_column(String(200))
    startup_prompt: Mapped[str | None] = mapped_column(Text)
    # ``NULL`` preserves the pre-FR-116 contract: all Context frozen in the
    # Snapshot was applied. New human starts persist an explicit list, where
    # an empty list deliberately means that no node Context is applied.
    context_ids_json: Mapped[list[str] | None] = mapped_column(JSON)
    # Immutable per-launch main Agent configuration. The binding copies and
    # freezes it when the automatic conversation is reserved.
    agent_preset_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Runtime gates are selected for one execution and frozen with its Attempt.
    # A reusable Flow definition intentionally does not own this configuration.
    gate_policies_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    output_targets_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint(
            "flow_run_id", "field_key", "version_no", name="uq_artifact_field_version"
        ),
        CheckConstraint(
            "inline_content IS NOT NULL OR uri IS NOT NULL OR storage_key IS NOT NULL",
            name="ck_artifact_has_content",
        ),
        CheckConstraint("byte_size >= 0", name="ck_artifact_size_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    producer_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # Human input is owned by one declared node contract. It is intentionally
    # not a FlowRun-wide pool that other nodes may browse or bind.
    consumer_node_key: Mapped[str | None] = mapped_column(String(100), index=True)
    field_key: Mapped[str] = mapped_column(String(100))
    version_no: Mapped[int] = mapped_column(Integer)
    artifact_type: Mapped[str] = mapped_column(String(80))
    storage_key: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)
    inline_content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(100), default="text/plain")
    source: Mapped[str] = mapped_column(String(30))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AttemptInputBinding(Base):
    __tablename__ = "attempt_input_bindings"
    __table_args__ = (
        UniqueConstraint("attempt_id", "input_field_key", name="uq_attempt_input_field"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    input_field_key: Mapped[str] = mapped_column(String(100))
    artifact_version_id: Mapped[str] = mapped_column(String(36))
    binding_source: Mapped[str] = mapped_column(String(30), default="HUMAN")


class GateEvaluation(Base):
    __tablename__ = "gate_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "policy_snapshot_key",
            "stage",
            "evaluation_attempt",
            name="uq_gate_evaluation_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    policy_snapshot_key: Mapped[str] = mapped_column(String(100))
    stage: Mapped[str] = mapped_column(String(10))
    policy_position: Mapped[int] = mapped_column(Integer)
    evaluation_attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(10))
    input_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    log_excerpt: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class HumanAction(Base):
    __tablename__ = "human_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action_type: Mapped[str] = mapped_column(String(60))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_event_cursor", "flow_run_id", "cursor"),)

    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str] = mapped_column(String(36), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = (
    "FlowRun",
    "RunSnapshot",
    "NodeRun",
    "NodeAttempt",
    "ArtifactVersion",
    "AttemptInputBinding",
    "GateEvaluation",
    "HumanAction",
    "RunEvent",
)
