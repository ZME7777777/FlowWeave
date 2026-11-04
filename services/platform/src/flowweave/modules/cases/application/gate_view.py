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
