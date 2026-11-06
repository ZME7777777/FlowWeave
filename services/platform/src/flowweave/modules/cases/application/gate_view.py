"""Read-only projection of gate policies and evaluations for the Case coordinator.

Every gate type (platform output/business-result contracts, author Python and Prompt gates)
persists the same ``GateResult`` shape in ``GateEvaluation``, so one projection covers them all.
Text may echo Agent output and is therefore bounded, untrusted data.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.runs.infrastructure.models import GateEvaluation, NodeAttempt

_GATE_TEXT_LIMIT = 1500
_GATE_LIST_LIMIT = 20


def clip(value: Any, depth: int = 0) -> Any:
    """Bound gate payloads: Agent-authored text is data, never unbounded or an instruction."""

    if isinstance(value, str):
        return (
            value if len(value) <= _GATE_TEXT_LIMIT else value[:_GATE_TEXT_LIMIT] + "…[truncated]"
        )
    if depth >= 6:
        return None
    if isinstance(value, list):
        return [clip(item, depth + 1) for item in value[:_GATE_LIST_LIMIT]]
    if isinstance(value, dict):
        return {str(key): clip(item, depth + 1) for key, item in list(value.items())[:50]}
    return value


def gate_summary(evaluation: GateEvaluation | None) -> dict[str, Any] | None:
    if evaluation is None:
        return None
    return {
        "evaluation_id": evaluation.id,
        "stage": evaluation.stage,
        "decision": evaluation.decision,
        "error_code": evaluation.error_code,
        "summary": clip(str((evaluation.result_json or {}).get("summary") or "")),
    }


def latest_gate_evaluations(db: Session, attempt_id: str) -> list[GateEvaluation]:
    """Newest evaluation of every (policy, stage) for an Attempt, in gate order."""

    latest: dict[tuple[str, str], GateEvaluation] = {}
    rows = db.scalars(
        select(GateEvaluation)
        .where(GateEvaluation.attempt_id == attempt_id)
        .order_by(GateEvaluation.evaluation_attempt, GateEvaluation.created_at)
    )
    for row in rows:
        latest[(row.policy_snapshot_key, row.stage)] = row
    return sorted(
        latest.values(), key=lambda row: (row.stage != "START", row.policy_position, row.id)
    )


def gate_detail(
    attempt: NodeAttempt, evaluations: list[GateEvaluation], *, history: dict[str, int]
) -> dict[str, Any]:
    """Frozen policy plus latest result for built-in (system_owned) and author gates alike."""

    policies = {
        str(policy.get("id")): policy
        for policy in cast(list[dict[str, Any]], attempt.gate_policies_json or [])
        if isinstance(policy, dict)
    }
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in evaluations:
        policy = policies.get(row.policy_snapshot_key, {})
        config = cast(dict[str, Any], policy.get("config") or {})
        result = cast(dict[str, Any], row.result_json or {})
        seen.add(row.policy_snapshot_key)
        gates.append(
            {
                "policy_id": row.policy_snapshot_key,
                "stage": row.stage,
                "gate_type": policy.get("gate_type"),
                "origin": "PLATFORM" if config.get("system_owned") else "AUTHOR",
                "enabled": policy.get("enabled", True),
                "policy_config": clip(config),
                "evaluation": {
                    "evaluation_id": row.id,
                    "evaluation_attempt": row.evaluation_attempt,
                    "evaluations_total": history.get(row.policy_snapshot_key, 1),
                    "candidate_output_set_id": row.candidate_output_set_id,
                    "decision": row.decision,
                    "error_code": row.error_code,
                    "summary": clip(result.get("summary")),
                    "reasons": clip(result.get("reasons") or []),
                    "evidence": clip(result.get("evidence") or []),
                    "details": clip(result.get("details") or {}),
                    "log_excerpt": clip(row.log_excerpt or ""),
                },
            }
        )
    for policy_id, policy in policies.items():
        if policy_id in seen:
            continue
        config = cast(dict[str, Any], policy.get("config") or {})
        gates.append(
            {
                "policy_id": policy_id,
                "stage": policy.get("stage"),
                "gate_type": policy.get("gate_type"),
                "origin": "PLATFORM" if config.get("system_owned") else "AUTHOR",
                "enabled": policy.get("enabled", True),
                "policy_config": clip(config),
                "evaluation": None,
            }
        )
    return {
        "attempt_id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "attempt_state": attempt.state,
        "attempt_error_code": attempt.error_code,
        "gates": gates,
    }


def failure_policy_state(db: Session, node_run_id: str) -> dict[str, Any] | None:
    """Author-declared handling for the node's latest END-gate failure, with rounds used.

    ``None`` means nothing was declared (or nothing failed): the historical behaviour applies.
    A rerun creates a new Attempt, so rounds used is the number of Attempts after the first.
    """

    from flowweave.shared.domain.gate_failure_policy import resolve

    attempts = list(
        db.scalars(
            select(NodeAttempt)
            .where(NodeAttempt.node_run_id == node_run_id)
            .order_by(NodeAttempt.attempt_no)
        )
    )
    if not attempts:
        return None
    latest = attempts[-1]
    failed = [
        row.policy_snapshot_key
        for row in latest_gate_evaluations(db, latest.id)
        if row.stage == "END" and row.decision == "FAIL"
    ]
    if not failed:
        return None
    declared = resolve(cast(list[dict[str, Any]], latest.gate_policies_json or []), failed)
    if declared is None:
        return None
    used = len(attempts) - 1
    state = declared.as_dict()
    state["rounds_used"] = used
    if declared.action == "RERUN":
        remaining = max(declared.max_rounds - used, 0)
        state["rounds_remaining"] = remaining
        state["exhausted"] = remaining == 0
        state["instruction"] = (
            "流程作者声明：此门禁失败时应重跑该节点"
            f"（最多 {declared.max_rounds} 轮，已用 {used} 轮）。"
            + ("重跑次数已用尽，请用 ask_user 交由用户决定。" if remaining == 0 else "")
        )
    elif declared.action == "STOP":
        state["instruction"] = "流程作者声明：此门禁失败时应终止本次执行，不得重跑。"
    return state


def enforce_failure_policy(db: Session, node_run_id: str, action: str) -> None:
    """Reject a rerun the flow designer did not allow (or has no rounds left for)."""

    from flowweave.shared.errors import DomainError

    state = failure_policy_state(db, node_run_id)
    if state is None:
        return
    if state["action"] == "STOP":
        raise DomainError(
            "CASE_GATE_POLICY_STOP",
            "流程作者声明此门禁失败应终止执行，不允许重跑；请停止执行或用 ask_user 询问用户",
            409,
            {"policy": state},
        )
    if state["action"] == "RERUN" and state["exhausted"]:
        raise DomainError(
            "CASE_GATE_POLICY_RERUN_EXHAUSTED",
            "作者声明的重跑次数已用尽，请用 ask_user 交由用户决定",
            409,
            {"policy": state},
        )
