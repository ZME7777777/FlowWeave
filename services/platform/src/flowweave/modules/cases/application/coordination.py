"""Case-owned coordination occurrences; OpenHands owns conversation execution."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import conversations
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.cases.application import service
from flowweave.modules.cases.infrastructure.models import Case, CaseControlRound, CaseStage
from flowweave.modules.tasks.public import enqueue
from flowweave.modules.users.application.security import user_runtime_project_root
from flowweave.shared.application.transactions import finish
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.schemas import CaseControlConversationWrite


def _enqueue(db: Session, entry: CaseControlRound) -> None:
    enqueue(
        db,
        task_type="BOOTSTRAP_CASE_CONTROL_ROUND",
        aggregate_type="CASE_CONTROL_ROUND",
        aggregate_id=entry.id,
        idempotency_key=f"case-round:{entry.id}:{entry.delivery_attempt}",
    )


def _snapshot(item: Case, stages: list[CaseStage]) -> dict[str, Any]:
    return deepcopy(
        {
            "case_id": item.id,
            "name": item.name,
            "description": item.description,
            "objective": item.objective,
            "state": item.state,
            "row_version": item.row_version,
            "materials": item.materials_json or [],
            "stages": [
                {
                    "id": stage.id,
                    "stage_key": stage.stage_key,
                    "name": stage.name,
                    "objective": stage.objective,
                    "position": stage.position,
                    "state": stage.state,
                    "execution_mode": stage.execution_mode,
                    "template_flow_run_id": stage.flow_run_id,
                    "current_execution_flow_run_id": stage.current_execution_flow_run_id,
                }
                for stage in stages
            ],
        }
    )


def _prompt(snapshot: dict[str, Any], workspace_id: str, round_id: str) -> str:
    return (
        f"你是场景“{snapshot['name']}”的协调 Agent。本协调轮次：{round_id}。\n"
        "以下是本轮启动时冻结的场景目标、说明、完整有序路径与资料目录，资料正文按需读取。\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + f"\n工作目录：{user_runtime_project_root(workspace_id)}"
        f"/.flowweave/cases/{snapshot['case_id']}。\n"
        "case.toml 是平台投影的最新场景状态，memory.md 用于可复用工作记忆；"
        "启动快照可能过时，行动前必须核对最新事实。LINK 是资料原始链接，FILE 是本场景资料路径。\n"
        "先判断目标、资料与节点输入是否匹配；会话已启动不代表流程已启动或业务输入已满足。"
        "仅使用本会话实际提供的正式工具和平台受控动作，在当前活动阶段的独立执行副本上工作。"
        "不得直接修改原模板、历史副本、已启动连续运行或其他场景；不得把文字建议当作已执行动作。"
        "工具或资料不可用时明确记录阻塞，不得编造工具调用、访问凭据或执行结果。"
        "每个节点结果需要业务复核，再判断补充输入、重跑、接受或进入后续阶段；场景目标达成需有正式证据。"
    )


def reserve_control_round(
    db: Session, case_id: str, payload: CaseControlConversationWrite, idempotency_key: str
) -> dict[str, Any]:
    if not idempotency_key.strip() or len(idempotency_key) > 140:
        raise DomainError("CASE_AGENT_COMMAND_INVALID", "启动请求标识长度应为 1–140", 422)
    item = db.scalar(
        select(Case)
        .where(Case.id == case_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise not_found("case", case_id)
    existing = db.scalar(
        select(CaseControlRound)
        .where(
            CaseControlRound.case_id == case_id, CaseControlRound.idempotency_key == idempotency_key
        )
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        # A replay is a read of the original occurrence, never a new start or
        # a way to reselect a superseded historical coordinator.
        if existing.id == item.current_control_round_id and existing.state == "FAILED":
            service.require_case_action(item, "START_AGENT")
            service.validate_case_session_config(
                db, CaseControlConversationWrite.model_validate(existing.session_config_json)
            )
            existing.state = "PENDING"
            existing.error_code = None
            existing.delivery_attempt += 1
            _enqueue(db, existing)
            item.row_version += 1
            finish(db)
        return service.get_case(db, item.id)
    service.require_case_action(item, "START_AGENT")
    if item.current_control_round_id:
        previous = db.get(CaseControlRound, item.current_control_round_id)
        if previous and (
            previous.state == "PENDING"
            or (
                previous.state == "FAILED" and db.get(AgentConversationBinding, previous.binding_id)
            )
        ):
            raise DomainError("CASE_AGENT_START_PENDING", "请先重试并核对当前协调轮次", 409)
    stages = list(
        db.scalars(
            select(CaseStage)
            .where(CaseStage.case_id == item.id)
            .order_by(CaseStage.position, CaseStage.id)
        )
    )
    service.validate_case_start_path(db, stages)
    saved = item.session_config_json or {}
    if not saved.get("model_provider_id") or not saved.get("model_name"):
        raise DomainError("CASE_AGENT_CONFIG_REQUIRED", "请先保存 Agent 配置", 409)
    config = CaseControlConversationWrite.model_validate(saved)
    if config.model_dump() != payload.model_dump():
        raise DomainError("CASE_AGENT_CONFIG_CHANGED", "Agent 配置已更新，请刷新后重试", 409)
    validated = service.validate_case_session_config(db, config)
    workspace = service.agent_workspaces.ensure_default_agent_workspace(db)
    if item.control_conversation_binding_id:
        conversations.assert_conversation_stopped(
            db, workspace.id, item.control_conversation_binding_id
        )
    if not item.control_work_directory_id:
        directory = service.agent_workspaces.create_case_work_directory(
            db, workspace.id, item.id, f"事项 · {item.name}"
        )
        item.control_work_directory_id = str(directory["id"])
    snapshot = _snapshot(item, stages)
    round_id = str(uuid4())
    entry = CaseControlRound(
        id=round_id,
        case_id=item.id,
        workspace_id=workspace.id,
        work_directory_id=item.control_work_directory_id,
        binding_id=str(uuid4()),
        idempotency_key=idempotency_key,
        state="PENDING",
        delivery_attempt=1,
        session_config_json=deepcopy(validated),
        context_snapshot_json=snapshot,
        initial_prompt=_prompt(snapshot, workspace.id, round_id),
    )
    db.add(entry)
    db.flush()
    item.current_control_round_id = entry.id
    item.control_conversation_binding_id = None
    item.row_version += 1
    _enqueue(db, entry)
    finish(db)
    return service.get_case(db, item.id)


@contextmanager
def _delivery_lock(db: Session, round_id: str):
    """Native bootstrap commits internally; retain a session lock across them."""
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as connection:
        lock_id = connection.scalar(select(func.hashtextextended(f"case-bootstrap:{round_id}", 0)))
        acquired = connection.scalar(select(func.pg_try_advisory_lock(lock_id)))
        connection.commit()
        if not acquired:
            raise DomainError("CASE_AGENT_DELIVERY_BUSY", "协调轮次正在创建", 409)
        try:
            yield
        finally:
            connection.rollback()
            connection.scalar(select(func.pg_advisory_unlock(lock_id)))
            connection.commit()


def deliver_control_round(db: Session, round_id: str) -> None:
    with _delivery_lock(db, round_id):
        entry = db.get(CaseControlRound, round_id, populate_existing=True)
        if entry is None or entry.state != "PENDING":
            return
        item = db.get(Case, entry.case_id, populate_existing=True)
        if item is None or item.current_control_round_id != entry.id:
            entry.state, entry.error_code = "FAILED", "CASE_AGENT_ROUND_SUPERSEDED"
            db.flush()
            return
        try:
            service.require_case_action(item, "START_AGENT")
            stages = list(
                db.scalars(
                    select(CaseStage)
                    .where(CaseStage.case_id == item.id)
                    .order_by(CaseStage.position, CaseStage.id)
                )
            )
            service.validate_case_start_path(db, stages)
            if _snapshot(item, stages)["stages"] != entry.context_snapshot_json["stages"]:
                raise DomainError("CASE_AGENT_PATH_CHANGED", "场景路径已变化，请新建协调轮次", 409)
            config = service.validate_case_session_config(
                db, CaseControlConversationWrite.model_validate(entry.session_config_json)
            )
            service.write_case_files(db, item)
            # Native bootstrap's reservation commit publishes this scope before
            # its first user event can run tools. Browsing only exposes STARTED.
            item.control_conversation_binding_id = entry.binding_id
            created = conversations.bootstrap_conversation(
                db,
                entry.workspace_id,
                work_directory_id=entry.work_directory_id,
                conversation_id=entry.binding_id,
                model_provider_id=config["model_provider_id"],
                model_name=config["model_name"],
                reasoning_effort=config["reasoning_effort"],
                capability_version_ids=tuple(config["capability_version_ids"]),
                credential_ids=tuple(config["credential_ids"]),
                content=entry.initial_prompt,
                idempotency_key=f"case-round:{entry.id}",
            )
            if str(created["conversation"]["id"]) != entry.binding_id:
                raise DomainError("CASE_AGENT_BINDING_MISMATCH", "协调会话身份校验失败", 409)
            # Native I/O releases the bootstrap transaction; preserve intervening
            # material/configuration edits and their optimistic-lock version.
            db.refresh(item, with_for_update=True)
            entry.state = "STARTED"
            entry.message_cursor = created.get("cursor")
            entry.started_at = now()
            entry.error_code = None
            item.control_conversation_binding_id = entry.binding_id
            item.row_version += 1
        except DomainError as exc:
            # Native bootstrap preserves ambiguous command/event identities.
            # Retrying this occurrence always passes the same UUID and key.
            db.refresh(item, with_for_update=True)
            entry.state, entry.error_code = "FAILED", exc.code
            item.control_conversation_binding_id = None
            item.row_version += 1
        db.flush()


def get_control_round(db: Session, case_id: str, round_id: str) -> dict[str, Any]:
    if db.get(Case, case_id) is None:
        raise not_found("case", case_id)
    entry = db.get(CaseControlRound, round_id)
    if entry is None or entry.case_id != case_id:
        raise not_found("case_control_round", round_id)
    return {
        "id": entry.id,
        "state": entry.state,
        "session_config": entry.session_config_json,
        "context_snapshot": entry.context_snapshot_json,
        "message_cursor": entry.message_cursor,
        "error_code": entry.error_code,
    }


def retry_control_round(db: Session, case_id: str, round_id: str) -> dict[str, Any]:
    item = db.get(Case, case_id)
    if item is None:
        raise not_found("case", case_id)
    entry = db.get(CaseControlRound, round_id)
    if entry is None or entry.case_id != case_id:
        raise not_found("case_control_round", round_id)
    if entry.id != item.current_control_round_id or entry.state not in {"FAILED", "PENDING"}:
        raise DomainError("CASE_AGENT_ROUND_NOT_RETRYABLE", "该协调轮次不可重试", 409)
    return reserve_control_round(
        db,
        case_id,
        CaseControlConversationWrite.model_validate(entry.session_config_json),
        entry.idempotency_key,
    )
