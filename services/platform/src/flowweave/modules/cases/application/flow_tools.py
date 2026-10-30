"""Governed workflow tools; targets derive from the current native coordinator."""

from __future__ import annotations

import copy
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.cases.application import native_tools, service
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlPlan,
    CaseFlowToolGrant,
    CaseStage,
)
from flowweave.modules.runs.infrastructure.models import FlowRun, NodeAttempt, NodeRun, RunSnapshot
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.credentials_crypto import decrypt_secret
from flowweave.shared.errors import DomainError, conflict
from flowweave.shared.schemas import (
    AutomaticRunDraftUpdateWrite,
    CaseControlPlanWrite,
    StepwiseNodeDraftWrite,
)


class FlowReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stage_key: str | None = Field(default=None, max_length=100)
    source: Literal["TEMPLATE", "CURRENT"] = "CURRENT"
    section: Literal["overview", "nodes", "edges", "drafts"] = "overview"
    flow_node_key: str | None = Field(default=None, max_length=100)
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=20, ge=1, le=50)


class FlowActionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "CREATE_EXECUTION_COPY",
        "CONFIGURE_EXECUTION",
        "START_EXECUTION",
        "START_STEPWISE_NODE",
        "ACCEPT_STEPWISE_ATTEMPT",
        "REJECT_STEPWISE_ATTEMPT",
        "RERUN_STEPWISE_NODE",
    ]
    idempotency_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,119}$")
    reason: str = Field(min_length=1, max_length=2_000)
    expected_case_row_version: int = Field(ge=1)
    expected_stage_key: str = Field(min_length=1, max_length=100)
    expected_execution_flow_run_id: str | None = Field(default=None, max_length=36)
    expected_run_row_version: int = Field(ge=1)
    expected_snapshot_version: int = Field(ge=1)
    flow_node_key: str | None = Field(default=None, min_length=1, max_length=100)
    node_run_id: str | None = Field(default=None, min_length=1, max_length=36)
    node_attempt_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_attempt_state_version: int | None = Field(default=None, ge=1)
    copy_input_bindings: bool | None = None
    draft_row_version: int | None = Field(default=None, ge=1)
    automatic_draft: AutomaticRunDraftUpdateWrite | None = None
    stepwise_draft: StepwiseNodeDraftWrite | None = None
    retry_of_plan_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_action(self) -> FlowActionArguments:
        if not self.reason.strip():
            raise ValueError("reason is required")
        configuring = self.action == "CONFIGURE_EXECUTION"
        if configuring != (self.automatic_draft is not None or self.stepwise_draft is not None):
            raise ValueError("configuration is required only for CONFIGURE_EXECUTION")
        if self.automatic_draft and self.stepwise_draft:
            raise ValueError("choose one draft mode")
        node_actions = {
            "START_STEPWISE_NODE",
            "ACCEPT_STEPWISE_ATTEMPT",
            "REJECT_STEPWISE_ATTEMPT",
            "RERUN_STEPWISE_NODE",
        }
        node_action = self.stepwise_draft is not None or self.action in node_actions
        if node_action != (self.flow_node_key is not None):
            raise ValueError("flow_node_key is required only for a stepwise node action")
        if (self.action == "START_STEPWISE_NODE") != (self.draft_row_version is not None):
            raise ValueError("draft_row_version is required only for START_STEPWISE_NODE")
        review_actions = {"ACCEPT_STEPWISE_ATTEMPT", "REJECT_STEPWISE_ATTEMPT"}
        if (self.action in review_actions) != (
            self.node_run_id is not None
            and self.node_attempt_id is not None
            and self.expected_attempt_state_version is not None
        ):
            raise ValueError("review actions require frozen node and Attempt identities")
        if (self.action == "RERUN_STEPWISE_NODE") != (
            self.node_run_id is not None and self.node_attempt_id is None
        ):
            raise ValueError("rerun requires only the accepted node_run_id")
        if self.action != "REJECT_STEPWISE_ATTEMPT" and self.copy_input_bindings is not None:
            raise ValueError("copy_input_bindings is available only when rejecting an Attempt")
        if (
            self.action == "CREATE_EXECUTION_COPY"
            and self.expected_execution_flow_run_id is not None
        ):
            raise ValueError("copy creation requires no current execution")
        if self.action != "CREATE_EXECUTION_COPY" and self.expected_execution_flow_run_id is None:
            raise ValueError("an expected execution is required")
        if (
            self.automatic_draft
            and self.automatic_draft.expected_row_version != self.expected_run_row_version
        ):
            raise ValueError("automatic draft version differs from run version")
        return self


def _tool(name: str, description: str, schema: type[BaseModel], readonly: bool) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema.model_json_schema(),
        "annotations": {
            "readOnlyHint": readonly,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        "read_case_flow",
        "Read this scene's template or execution: overview, graph input/output contracts, "
        "edges, and editable drafts. Use returned versions as action preconditions. "
        "No files are downloaded.",
        FlowReadArguments,
        True,
    ),
    _tool(
        "manage_case_flow",
        "Create or configure the ACTIVE stage copy, start a stepwise node, formally accept or "
        "reject its reviewed Attempt, or reopen an accepted predecessor for an editable rerun. "
        "Targets derive from this coordinator. Read fresh versions first. Rejection creates the "
        "formal replacement Attempt; editable reruns are limited to unconsumed predecessors. "
        "Reuse one idempotency_key after timeouts. Failed actions require retry_of_plan_id and "
        "a new key. Does not advance stages or start successors implicitly.",
        FlowActionArguments,
        False,
    ),
]


def _pick(value: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: copy.deepcopy(value[key]) for key in keys if key in value}


def _preset(value: Any) -> dict[str, Any]:
    return _pick(
        value or {},
        (
            "model_provider_id",
            "model_name",
            "reasoning_effort",
            "capability_version_ids",
            "node_context_enabled",
            "node_context_prompt",
            "fallback_models",
        ),
    )


def _draft(value: dict[str, Any]) -> dict[str, Any]:
    result = _pick(
        value,
        (
            "row_version",
            "rerun_source_node_run_id",
            "startup_mode",
            "startup_prompt",
            "artifact_ids",
            "input_urls",
            "context_ids",
        ),
    )
    result["agent_preset"] = _preset(value.get("agent_preset"))
    result["gates"] = [
        {
            **_pick(
                gate, ("stage", "position", "gate_type", "enabled", "timeout_seconds", "config")
            ),
            "agent_preset": _preset(gate.get("agent_preset")),
        }
        for gate in value.get("gates", [])
    ]
    # Preserve only artifact references, never storage paths or Runtime manifests.
    result["input_bindings"] = {
        key: _pick(binding, ("artifact_version_id", "binding_source"))
        for key, binding in cast(
            dict[str, dict[str, Any]], value.get("input_bindings") or {}
        ).items()
    }
    return result


def read_flow(db: Session, token: str, args: FlowReadArguments) -> dict[str, Any]:
    owner, _ = native_tools.token_identity(token, contract=native_tools.FLOWS)
    with tenant_user(owner):
        _, item = native_tools.authenticate(db, token, contract=native_tools.FLOWS)
        query = select(CaseStage).where(CaseStage.case_id == item.id)
        query = (
            query.where(CaseStage.stage_key == args.stage_key)
            if args.stage_key
            else query.where(CaseStage.state == "ACTIVE")
        )
        stage = db.scalar(query.order_by(CaseStage.position))
        if stage is None:
            raise conflict("stage is not in this scene")
        run_id = (
            stage.flow_run_id if args.source == "TEMPLATE" else stage.current_execution_flow_run_id
        )
        run = db.get(FlowRun, run_id) if run_id else None
        common: dict[str, Any] = {
            "case_row_version": item.row_version,
            "stage_key": stage.stage_key,
            "stage_state": stage.state,
            "execution_mode": stage.execution_mode,
            "current_execution_flow_run_id": stage.current_execution_flow_run_id,
            "source": args.source,
        }
        if run is not None and run.owner_user_id != item.owner_user_id:
            raise native_tools.denied()
        if run is None:
            return {**common, "flow_run": None}
        snapshot = db.get(RunSnapshot, run.active_snapshot_id) if run.active_snapshot_id else None
        if snapshot is None or snapshot.flow_run_id != run.id:
            raise conflict("flow snapshot is unavailable")
        common.update(
            {
                "flow_run": {
                    "id": run.id,
                    "name": run.name,
                    "state": run.state,
                    "run_mode": run.run_mode,
                    "row_version": run.row_version,
                    "active_snapshot_version": snapshot.version,
                    "environment_version_id": run.environment_version_id,
                },
                "start_node_key": (run.automation_plan_json or {}).get("start_node_key"),
            }
        )
        definition = snapshot.definition_json
        nodes = definition.get("nodes", [])
        if args.section == "overview":
            return {
                **common,
                "node_count": len(nodes),
                "edge_count": len(definition.get("edges", [])),
                "writable": args.source == "CURRENT"
                and stage.state == "ACTIVE"
                and item.state != "PAUSED"
                and (
                    run.state == "DRAFT"
                    if run.run_mode == "AUTOMATIC"
                    else run.state not in {"COMPLETED", "CANCELLED"}
                ),
            }
        if args.section == "nodes":
            entries: list[dict[str, Any]] = []
            current_nodes = {
                record.flow_node_snapshot_key: record
                for record in db.scalars(
                    select(NodeRun)
                    .where(NodeRun.flow_run_id == run.id)
                    .order_by(NodeRun.sequence_no)
                )
            }
            latest_attempts: dict[str, Any] = {}
            for execution in current_nodes.values():
                attempt = db.scalar(
                    select(NodeAttempt)
                    .where(NodeAttempt.node_run_id == execution.id)
                    .order_by(NodeAttempt.attempt_no.desc())
                    .limit(1)
                )
                latest_attempts[execution.id] = (
                    {
                        "id": attempt.id,
                        "attempt_no": attempt.attempt_no,
                        "state": attempt.state,
                        "state_version": attempt.state_version,
                        "candidate_output_set_id": attempt.current_candidate_output_set_id,
                        "error_code": attempt.error_code,
                    }
                    if attempt is not None
                    else None
                )
            for node in nodes:
                if args.flow_node_key and node.get("instance_key") != args.flow_node_key:
                    continue
                asset = cast(dict[str, Any], node.get("asset") or {})
                execution = current_nodes.get(node.get("instance_key"))
                entries.append(
                    {
                        **_pick(node, ("instance_key", "alias", "runtime_port_overrides")),
                        "execution": (
                            {
                                "node_run_id": execution.id,
                                "state": execution.state,
                                "accepted_attempt_id": execution.accepted_attempt_id,
                                "latest_attempt": latest_attempts[execution.id],
                            }
                            if execution
                            else None
                        ),
                        "asset": {
                            **_pick(asset, ("name", "description", "inputs", "outputs")),
                            "executor": _pick(
                                asset.get("executor") or {}, ("startup_prompt", "context_prompt")
                            ),
                        },
                    }
                )
        elif args.section == "edges":
            entries = [
                _pick(
                    edge,
                    (
                        "source_node_key",
                        "target_node_key",
                        "source_instance_key",
                        "target_instance_key",
                        "source_output_key",
                        "target_input_key",
                        "source",
                        "target",
                        "kind",
                    ),
                )
                for edge in definition.get("edges", [])
            ]
            # Port mappings are a separate graph collection in the frozen Flow definition.
            entries += [
                {
                    "kind": "PORT_MAPPING",
                    **_pick(
                        mapping,
                        (
                            "source_instance_key",
                            "source_output_key",
                            "target_instance_key",
                            "target_input_key",
                        ),
                    ),
                }
                for mapping in definition.get("port_mappings", [])
            ]
        else:
            plan = run.automation_plan_json or {}
            drafts = plan.get("node_plans" if run.run_mode == "AUTOMATIC" else "node_drafts", {})
            entries = [
                {"flow_node_key": key, **_draft(value)}
                for key, value in sorted(drafts.items())
                if not args.flow_node_key or key == args.flow_node_key
            ]
        end = args.offset + args.limit
        return {
            **common,
            "section": args.section,
            "items": entries[args.offset : end],
            "total": len(entries),
            "next_offset": end if end < len(entries) else None,
        }


def action_result(plan: CaseControlPlan) -> dict[str, Any]:
    # Full orchestration projections may contain internal Runtime identities.
    # Tools receive the durable action identity and can re-read current facts.
    return {
        "plan_id": plan.id,
        "action": plan.action,
        "state": plan.state,
        "execution_flow_run_id": plan.execution_flow_run_id,
        "error_code": plan.error_code,
        "retry_of_plan_id": plan.retry_of_plan_id,
        "validation": plan.result_json.get("validation", {}),
    }


def manage_flow(db: Session, token: str, args: FlowActionArguments) -> dict[str, Any]:
    owner, _ = native_tools.token_identity(token, contract=native_tools.FLOWS)
    with tenant_user(owner):
        grant, item = native_tools.authenticate(db, token, contract=native_tools.FLOWS)
        # Serialize coordinator writes and path changes. Re-authorize after waiting.
        db.refresh(item, with_for_update=True)
        grant, item = native_tools.authenticate(db, token, contract=native_tools.FLOWS)
        service.require_case_action(item, args.action)
        request = args.model_dump(
            mode="json", exclude={"idempotency_key", "reason", "action", "retry_of_plan_id"}
        )
        existing = db.scalar(
            select(CaseControlPlan).where(
                CaseControlPlan.case_id == item.id,
                CaseControlPlan.idempotency_key == args.idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.source_binding_id != grant.binding_id
                or existing.action != args.action
                or existing.reason != args.reason.strip()
                or existing.input_snapshot_json.get("requested") != request
                or existing.retry_of_plan_id != args.retry_of_plan_id
            ):
                raise conflict("idempotency key is already bound to another action payload")
            if existing.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return action_result(existing)
            plan = existing
        else:
            scope = service.resolve_case_control_binding_scope(db, grant.binding_id)
            stage = scope["stage"]
            if args.retry_of_plan_id:
                failed = db.get(CaseControlPlan, args.retry_of_plan_id)
                if (
                    failed is None
                    or failed.state != "FAILED"
                    or failed.source_binding_id != grant.binding_id
                    or failed.case_id != item.id
                    or failed.stage_id != stage.id
                    or failed.action != args.action
                    or failed.execution_flow_run_id != stage.current_execution_flow_run_id
                    or failed.node_run_id != args.node_run_id
                    or failed.node_attempt_id != args.node_attempt_id
                ):
                    raise conflict("retry does not match a failed action in the current scope")
            # A failed precondition is an audited action too; check it at dispatch.
            payload = CaseControlPlanWrite(
                stage_id=stage.id,
                action=args.action,
                idempotency_key=args.idempotency_key,
                reason=args.reason,
                node_run_id=args.node_run_id,
                node_attempt_id=args.node_attempt_id,
                input_snapshot=request,
            )
            saved = service.create_case_control_plan_for_binding(
                db, grant.binding_id, payload, retry_of_plan_id=args.retry_of_plan_id
            )
            plan = db.get(CaseControlPlan, saved["id"])
            assert plan is not None
        try:
            service.execute_case_control_plan(db, item.id, plan.id)
        except service.CaseActionFailure:
            # The MCP transport commits this failed result before returning isError.
            pass
        return action_result(plan)


def validate_action_preconditions(
    db: Session, item: Case, stage: CaseStage, plan: CaseControlPlan
) -> FlowRun | None:
    raw = plan.input_snapshot_json.get("requested", {})
    if "expected_case_row_version" not in raw:
        if plan.action == "CONFIGURE_EXECUTION":
            raise conflict("configuration requires frozen action preconditions")
        return None  # Existing human/API plans keep their established contract.
    grant = db.scalar(
        select(CaseFlowToolGrant).where(CaseFlowToolGrant.binding_id == plan.source_binding_id)
    )
    if grant is None:
        raise native_tools.denied()
    native_tools.authenticate(
        db, decrypt_secret(grant.encrypted_token), contract=native_tools.FLOWS
    )
    args = FlowActionArguments.model_validate(
        {
            "action": plan.action,
            "idempotency_key": plan.idempotency_key,
            "reason": plan.reason,
            "retry_of_plan_id": plan.retry_of_plan_id,
            **raw,
        }
    )
    if (
        args.expected_case_row_version != item.row_version
        or args.expected_stage_key != stage.stage_key
        or args.expected_execution_flow_run_id != stage.current_execution_flow_run_id
    ):
        raise DomainError(
            "CASE_FLOW_VERSION_CONFLICT", "场景或当前执行已变化，请重新读取后再决定", 409
        )
    run_id = (
        stage.flow_run_id
        if plan.action == "CREATE_EXECUTION_COPY"
        else stage.current_execution_flow_run_id
    )
    run = (
        db.scalar(
            select(FlowRun)
            .where(FlowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run_id
        else None
    )
    snapshot = (
        db.get(RunSnapshot, run.active_snapshot_id) if run and run.active_snapshot_id else None
    )
    if (
        run is None
        or snapshot is None
        or run.owner_user_id != item.owner_user_id
        or snapshot.flow_run_id != run.id
        or run.row_version != args.expected_run_row_version
        or snapshot.version != args.expected_snapshot_version
    ):
        raise DomainError("CASE_FLOW_VERSION_CONFLICT", "流程版本已变化，请重新读取后再决定", 409)
    if plan.action != "CREATE_EXECUTION_COPY":
        if stage.state != "ACTIVE" or stage.current_execution_flow_run_id != run.id:
            raise conflict("action is outside the active stage execution")
    if plan.action == "CONFIGURE_EXECUTION" and stage.execution_mode == "STEPWISE":
        if not args.stepwise_draft or not args.flow_node_key:
            raise conflict("stepwise configuration requires a node draft")
        materialized = db.scalar(
            select(NodeRun.id)
            .where(
                NodeRun.flow_run_id == run.id, NodeRun.flow_node_snapshot_key == args.flow_node_key
            )
            .limit(1)
        )
        if materialized is not None:
            raise conflict("only unstarted stepwise nodes may be configured")
    stepwise_actions = {
        "START_STEPWISE_NODE",
        "ACCEPT_STEPWISE_ATTEMPT",
        "REJECT_STEPWISE_ATTEMPT",
        "RERUN_STEPWISE_NODE",
    }
    if plan.action in stepwise_actions and stage.execution_mode != "STEPWISE":
        raise conflict("node action requires stepwise mode")
    if plan.action in {"ACCEPT_STEPWISE_ATTEMPT", "REJECT_STEPWISE_ATTEMPT"}:
        if not args.node_run_id or not args.node_attempt_id:
            raise conflict("review action lacks frozen execution identities")
        node_run = db.get(NodeRun, args.node_run_id)
        attempt = db.get(NodeAttempt, args.node_attempt_id)
        if (
            node_run is None
            or attempt is None
            or node_run.flow_run_id != run.id
            or node_run.flow_node_snapshot_key != args.flow_node_key
            or attempt.node_run_id != node_run.id
            or plan.node_run_id != node_run.id
            or plan.node_attempt_id != attempt.id
        ):
            raise DomainError(
                "CASE_FLOW_VERSION_CONFLICT", "待复核节点身份已变化，请重新读取后再决定", 409
            )
    elif plan.action == "RERUN_STEPWISE_NODE":
        if not args.node_run_id:
            raise conflict("rerun action lacks a frozen node identity")
        node_run = db.get(NodeRun, args.node_run_id)
        if (
            node_run is None
            or node_run.flow_run_id != run.id
            or node_run.flow_node_snapshot_key != args.flow_node_key
            or plan.node_run_id != node_run.id
        ):
            raise DomainError(
                "CASE_FLOW_VERSION_CONFLICT", "待重跑节点身份已变化，请重新读取后再决定", 409
            )
    return run


def configure_execution(db: Session, stage: CaseStage, plan: CaseControlPlan) -> dict[str, Any]:
    requested = plan.input_snapshot_json["requested"]
    run_id = stage.current_execution_flow_run_id
    assert run_id is not None
    if stage.execution_mode == "CONTINUOUS":
        raw = requested.get("automatic_draft")
        if raw is None or requested.get("stepwise_draft") is not None:
            raise conflict("continuous configuration requires an automatic draft")
        projection = service.orchestration.update_automatic_run_draft(
            db, run_id, AutomaticRunDraftUpdateWrite.model_validate(raw)
        )
    else:
        projection = service.orchestration.save_stepwise_node_draft(
            db,
            run_id,
            requested["flow_node_key"],
            StepwiseNodeDraftWrite.model_validate(requested["stepwise_draft"]),
        )
    return {"flow_run_id": run_id, "row_version": projection.get("row_version")}
