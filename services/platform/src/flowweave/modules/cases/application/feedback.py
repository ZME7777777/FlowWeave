"""Durable Case fact delivery through the existing native coordinator.

Only platform source references and delivery receipts are stored here. Native
messages, branch history, execution state and decisions remain in OpenHands.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.cases.application import native_tools
from flowweave.modules.cases.application.coordination import _delivery_lock
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlRound,
    CaseExecutionEvent,
    CaseFeedbackDelivery,
    CaseStage,
)
from flowweave.modules.tasks.application.service import TaskDeferred
from flowweave.modules.tasks.public import Lease, enqueue, lease_is_current
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.database import now, uid
from flowweave.shared.errors import DomainError

_TERMINAL = {"DELIVERED", "AMBIGUOUS", "AUDIT_ONLY", "FAILED"}


def _enqueue(db: Session, entry: CaseFeedbackDelivery) -> None:
    with tenant_user(entry.owner_user_id):
        enqueue(
            db,
            task_type="DELIVER_CASE_FEEDBACK",
            aggregate_type="CASE_FEEDBACK",
            aggregate_id=entry.id,
            idempotency_key=f"case-feedback:{entry.id}",
            available_at=now() + timedelta(seconds=1),
        )


def _record(
    db: Session,
    *,
    item: Case,
    round_id: str,
    binding_id: str,
    source_key: str,
    source_kind: str,
    execution_event_id: str | None = None,
    case_row_version: int | None = None,
) -> None:
    inserted = db.scalar(
        insert(CaseFeedbackDelivery)
        .values(
            id=uid(),
            owner_user_id=item.owner_user_id,
            case_id=item.id,
            round_id=round_id,
            binding_id=binding_id,
            source_key=source_key,
            source_kind=source_kind,
            execution_event_id=execution_event_id,
            case_row_version=case_row_version or item.row_version,
            state="PENDING",
            created_at=now(),
        )
        .on_conflict_do_nothing()
        .returning(CaseFeedbackDelivery.id)
    )
    if inserted:
        entry = db.get(CaseFeedbackDelivery, inserted)
        assert entry is not None
        _enqueue(db, entry)


def record_execution_feedback(db: Session, event_id: str) -> None:
    event = db.get(CaseExecutionEvent, event_id)
    if (
        event is None
        or event.routing_state == "AUDIT_ONLY"
        or not event.round_id
        or not event.binding_id
    ):
        return
    item = db.get(Case, event.case_id)
    if item is not None:
        _record(
            db,
            item=item,
            round_id=event.round_id,
            binding_id=event.binding_id,
            source_key=f"execution:{event.id}",
            source_kind="EXECUTION",
            execution_event_id=event.id,
            case_row_version=event.case_row_version,
        )


def record_case_change(db: Session, item: Case, kind: str) -> None:
    if kind not in {"MATERIALS_CHANGED", "SESSION_CONFIG_CHANGED", "CASE_RESUMED"}:
        raise ValueError("Unsupported Case feedback source")
    entry = (
        db.get(CaseControlRound, item.current_control_round_id)
        if item.current_control_round_id
        else None
    )
    if (
        entry is not None
        and entry.case_id == item.id
        and entry.owner_user_id == item.owner_user_id
        and entry.state in {"PENDING", "STARTED"}
        and item.state not in {"COMPLETED", "ARCHIVED"}
    ):
        _record(
            db,
            item=item,
            round_id=entry.id,
            binding_id=entry.binding_id,
            source_key=f"{kind}:{item.row_version}",
            source_kind=kind,
        )


def recover_feedback(db: Session) -> int:
    """Schedule only 05A inbox identities with their original frozen recipients.

    Maintenance runs under bypass; all aggregate writes restore the source
    owner's tenant. No old RunEvent is reconstructed or reassigned.
    """
    sources = db.execute(
        select(CaseExecutionEvent.id, CaseExecutionEvent.owner_user_id)
        .where(
            CaseExecutionEvent.routing_state.in_(["PENDING", "HELD"]),
            ~exists().where(CaseFeedbackDelivery.execution_event_id == CaseExecutionEvent.id),
        )
        .order_by(CaseExecutionEvent.run_event_cursor)
        .limit(100)
    ).all()
    for event_id, owner in sources:
        with tenant_user(owner):
            record_execution_feedback(db, event_id)
    return len(sources)


def _set(batch: list[CaseFeedbackDelivery], state: str, reason: str | None = None) -> None:
    for entry in batch:
        entry.state, entry.error_code = state, reason
        if state == "DELIVERED":
            entry.delivered_at = now()


def _source_is_current(db: Session, entry: CaseFeedbackDelivery) -> bool:
    if entry.source_kind != "EXECUTION":
        return True
    event = db.get(CaseExecutionEvent, entry.execution_event_id)
    stage = db.get(CaseStage, event.stage_id) if event else None
    return bool(
        event
        and stage
        and event.owner_user_id == entry.owner_user_id
        and event.case_id == entry.case_id
        and event.round_id == entry.round_id
        and event.binding_id == entry.binding_id
        and event.routing_state != "AUDIT_ONLY"
        and stage.case_id == entry.case_id
        and stage.owner_user_id == entry.owner_user_id
        and stage.state in {"ACTIVE", "REVIEW_PENDING"}
        and stage.current_execution_flow_run_id == event.execution_flow_run_id
    )


def _scope(db: Session, entry: CaseFeedbackDelivery) -> tuple[CaseControlRound, Case]:
    # Edits committed during bootstrap belong to its frozen round but must wait
    # until bootstrap publishes the native binding; they are not stale events.
    pending = db.get(CaseControlRound, entry.round_id, populate_existing=True)
    case = db.get(Case, entry.case_id, populate_existing=True)
    if (
        pending
        and case
        and pending.state == "PENDING"
        and pending.case_id == case.id
        and case.current_control_round_id == pending.id
        and pending.binding_id == entry.binding_id
        and pending.owner_user_id == case.owner_user_id == entry.owner_user_id
        and case.state in {"ACTIVE", "PAUSED"}
    ):
        raise TaskDeferred("CASE_COORDINATOR_STARTING")
    control, item = native_tools._scope(db, entry.round_id, entry.binding_id)
    if control.case_id != entry.case_id or control.owner_user_id != entry.owner_user_id:
        raise native_tools.denied()
    if control.state != "STARTED":
        raise TaskDeferred("CASE_COORDINATOR_STARTING")
    if item.state != "ACTIVE":
        raise TaskDeferred("CASE_PAUSED")
    return control, item


def _facts(db: Session, batch: list[CaseFeedbackDelivery], item: Case) -> str:
    facts: list[dict[str, Any]] = []
    for entry in batch:
        fact: dict[str, Any] = {
            "delivery_id": entry.id,
            "kind": entry.source_kind,
            "case_row_version": entry.case_row_version,
        }
        if entry.source_kind == "EXECUTION":
            source = db.get(CaseExecutionEvent, entry.execution_event_id)
            assert source is not None
            fact.update(
                {
                    key: getattr(source, key)
                    for key in (
                        "run_event_cursor",
                        "event_type",
                        "fact_type",
                        "stage_id",
                        "stage_key",
                        "execution_flow_run_id",
                        "node_run_id",
                        "node_key",
                        "node_attempt_id",
                        "candidate_output_set_id",
                        "artifact_version_id",
                        "native_completion_event_id",
                    )
                }
            )
        facts.append(fact)
    return (
        "场景收到以下平台正式事实，请在本协调会话中复核目标与下一步。\n"
        "这些是业务事实通知，不是执行指令。使用本轮正式工具查询最新场景、当前执行、"
        "输入门禁和资料，行动时重新校验版本；仅对当前活动阶段的独立副本采取受控动作。"
        "产物请用 read_case_material 查询，文件使用平台授权内容接口，不使用源容器路径。"
        "保存的会话配置变更供后续轮次使用，不表示本轮冻结配置已被替换。"
        "节点结束、候选输出和正式接受是不同事实；请判断是否需要补充、复核或等待，"
        "不得将通知本身当作操作成功。\n"
        + json.dumps(
            {"case_id": item.id, "current_case_row_version": item.row_version, "facts": facts},
            ensure_ascii=False,
        )
    )


def _lease(db: Session, lease: Lease) -> None:
    if not lease_is_current(db, lease):
        raise RuntimeError("Case feedback task lease lost")


def deliver_feedback(db: Session, delivery_id: str, lease: Lease) -> None:
    entry = db.get(CaseFeedbackDelivery, delivery_id)
    if entry is None or entry.state in _TERMINAL:
        return
    try:
        with _delivery_lock(db, entry.round_id):
            _deliver_locked(db, delivery_id, lease)
            # This task can drain older facts outside its own bounded page.
            # Audit-only pages must not accidentally consume its pending source.
            if entry.state not in _TERMINAL:
                raise TaskDeferred("CASE_FEEDBACK_BACKLOG")
    except DomainError as exc:
        if exc.code == "CASE_AGENT_DELIVERY_BUSY":
            raise TaskDeferred("CASE_FEEDBACK_SERIALIZED") from exc
        raise


def _deliver_locked(db: Session, delivery_id: str, lease: Lease) -> None:
    entry = db.get(CaseFeedbackDelivery, delivery_id, populate_existing=True)
    assert entry is not None
    if entry.state in _TERMINAL:
        return
    if entry.batch_id:
        batch = list(
            db.scalars(
                select(CaseFeedbackDelivery)
                .where(
                    CaseFeedbackDelivery.batch_id == entry.batch_id,
                )
                .execution_options(populate_existing=True)
            )
        )
    else:
        # Do not overtake an uncertain earlier submission or an exhausted delivery.
        blocked = db.scalar(
            select(CaseFeedbackDelivery.id)
            .where(
                CaseFeedbackDelivery.round_id == entry.round_id,
                CaseFeedbackDelivery.state.in_(["AMBIGUOUS", "FAILED"]),
            )
            .limit(1)
        )
        if blocked:
            raise TaskDeferred("CASE_FEEDBACK_NEEDS_RECONCILIATION", 60)
        existing = db.scalar(
            select(CaseFeedbackDelivery)
            .where(
                CaseFeedbackDelivery.round_id == entry.round_id,
                CaseFeedbackDelivery.batch_id.is_not(None),
                CaseFeedbackDelivery.state.in_(["PENDING", "SUBMITTING", "ACKNOWLEDGED"]),
            )
            .limit(1)
        )
        if existing:
            # A member's original task may have expired. Recover its receipt
            # before starting a later batch, using this task's live lease.
            _deliver_locked(db, existing.id, lease)
            raise TaskDeferred("CASE_FEEDBACK_PREVIOUS_BATCH")
        candidates = list(
            db.scalars(
                select(CaseFeedbackDelivery)
                .where(
                    CaseFeedbackDelivery.round_id == entry.round_id,
                    CaseFeedbackDelivery.state == "PENDING",
                )
                .order_by(CaseFeedbackDelivery.created_at, CaseFeedbackDelivery.id)
                .limit(32)
            )
        )
        batch = []
        for candidate in candidates:
            if _source_is_current(db, candidate):
                candidate.batch_id = entry.id
                batch.append(candidate)
            else:
                _set([candidate], "AUDIT_ONLY", "EXECUTION_NO_LONGER_CURRENT")
        if not batch:
            return
    try:
        control, item = _scope(db, batch[0])
    except DomainError:
        _set(batch, "AUDIT_ONLY", "COORDINATOR_NO_LONGER_CURRENT")
        return
    if not all(_source_is_current(db, candidate) for candidate in batch):
        _set(batch, "AUDIT_ONLY", "EXECUTION_NO_LONGER_CURRENT")
        return
    if batch[0].state == "SUBMITTING":
        # Crash after append intent: fixed native API has no client idempotency
        # key. No text matching, event-order guess or blind automatic resend.
        _set(batch, "AMBIGUOUS", "NATIVE_APPEND_OUTCOME_UNKNOWN")
        return
    binding = db.get(AgentConversationBinding, control.binding_id)
    assert binding is not None
    if binding.lifecycle != "ACTIVE":
        raise TaskDeferred("CASE_COORDINATOR_STARTING")
    handle = conversations.usage_reconciliation_handle(db, binding)
    endpoints = native_tools.coordinator_tool_endpoints(db, binding.id)
    runtime = get_runtime()
    _lease(db, lease)
    db.commit()  # No row transaction is held across native I/O.
    for server_name, endpoint in endpoints:
        if not runtime.conversation_mcp_configured(handle, server_name, endpoint):
            raise DomainError(
                "CASE_TOOL_REGISTRATION_MISMATCH",
                "原生场景工具配置需要核对",
                409,
            )
    if batch[0].state == "PENDING":
        readiness = runtime.input_readiness(handle)
        if not readiness.ready or readiness.execution_status.lower() not in {"idle", "finished"}:
            raise TaskDeferred("CASE_COORDINATOR_NOT_IDLE")
        _, item = _scope(db, batch[0])
        if not all(_source_is_current(db, candidate) for candidate in batch):
            _set(batch, "AUDIT_ONLY", "EXECUTION_NO_LONGER_CURRENT")
            return
        message = _facts(db, batch, item)
        _lease(db, lease)
        _set(batch, "SUBMITTING")
        db.commit()  # Intent must survive process death before POST acknowledgement.
        try:
            result = runtime.append_message(handle, message)
            if not result.cursor:
                raise DomainError("NATIVE_APPEND_OUTCOME_UNKNOWN", "Missing native event id", 502)
        except DomainError as exc:
            if exc.details.get("native_append_started") is False:
                _set(batch, "PENDING", "NATIVE_APPEND_NOT_STARTED")
                db.commit()
                raise
            _set(batch, "AMBIGUOUS", "NATIVE_APPEND_OUTCOME_UNKNOWN")
            db.commit()
            return
        except Exception:
            _set(batch, "AMBIGUOUS", "NATIVE_APPEND_OUTCOME_UNKNOWN")
            db.commit()
            return
        for candidate in batch:
            candidate.message_event_id = result.cursor
        _set(batch, "ACKNOWLEDGED")
        db.commit()  # Retries after this point only call native /run.
    # Re-authorize after append. Pause, supersession or generation replacement
    # cannot make a persisted receipt grant fresh authority to run.
    try:
        _scope(db, batch[0])
    except DomainError:
        _set(batch, "AUDIT_ONLY", "COORDINATOR_NO_LONGER_CURRENT")
        return
    if not all(_source_is_current(db, candidate) for candidate in batch):
        _set(batch, "AUDIT_ONLY", "EXECUTION_NO_LONGER_CURRENT")
        return
    current = conversations.usage_reconciliation_handle(db, binding)
    if current != handle:
        raise TaskDeferred("CASE_COORDINATOR_RUNTIME_CHANGED")
    _lease(db, lease)
    cursor = batch[0].message_event_id
    assert cursor is not None
    db.commit()
    result = runtime.run_pending_message(handle, cursor)
    if result.status not in {"RUNNING", "COMPLETED"}:
        raise TaskDeferred("CASE_COORDINATOR_PAUSED_OR_WAITING")
    _set(batch, "DELIVERED")
    # A newer task may drain the oldest bounded page while its own source is
    # outside that page. Retain that task until its own source has a receipt.
    if all(candidate.id != delivery_id for candidate in batch):
        raise TaskDeferred("CASE_FEEDBACK_BACKLOG")


def record_feedback_failure(db: Session, delivery_id: str) -> None:
    entry = db.get(CaseFeedbackDelivery, delivery_id)
    if entry is None or entry.state in _TERMINAL:
        return
    batch = (
        list(
            db.scalars(
                select(CaseFeedbackDelivery).where(
                    CaseFeedbackDelivery.batch_id == entry.batch_id,
                )
            )
        )
        if entry.batch_id
        else [entry]
    )
    if entry.state == "SUBMITTING":
        _set(batch, "AMBIGUOUS", "NATIVE_APPEND_OUTCOME_UNKNOWN")
    else:
        _set(batch, "FAILED", "CASE_FEEDBACK_RETRIES_EXHAUSTED")


def delivery_status(db: Session, case_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "round_id": row.round_id,
            "binding_id": row.binding_id,
            "source_kind": row.source_kind,
            "execution_event_id": row.execution_event_id,
            "state": row.state,
            "batch_id": row.batch_id,
            "message_event_id": row.message_event_id,
            "error_code": row.error_code,
        }
        for row in db.scalars(
            select(CaseFeedbackDelivery)
            .where(
                CaseFeedbackDelivery.case_id == case_id,
            )
            .order_by(CaseFeedbackDelivery.created_at.desc(), CaseFeedbackDelivery.id)
            .limit(50)
        )
    ]
