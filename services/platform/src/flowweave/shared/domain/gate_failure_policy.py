"""Author-declared handling of a business gate FAIL (pure rules, no I/O).

A flow designer decides, when designing the node, what a failed gate means: hand the
decision to a human/coordinator (``ESCALATE``, the historical default), ask for a bounded
number of reruns (``RERUN``), or end the execution (``STOP``). The platform only enforces
the declaration; it never forks or directs an author Conversation by itself (FR-241).

The declaration lives in the gate ``config`` and is therefore frozen with the Attempt's
gate policy snapshot, exactly like the rest of the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

OnFailAction = Literal["ESCALATE", "RERUN", "STOP"]
ACTIONS: frozenset[str] = frozenset({"ESCALATE", "RERUN", "STOP"})
MAX_ROUNDS_LIMIT = 3
DEFAULT_ROUNDS = 1


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    action: OnFailAction
    max_rounds: int
    note: str
    policy_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "action": self.action,
            "max_rounds": self.max_rounds if self.action == "RERUN" else None,
            "note": self.note,
        }


def validate_on_fail(value: object) -> str | None:
    """Return a human-readable problem, or ``None`` when the declaration is valid."""

    if value is None:
        return None
    if not isinstance(value, dict):
        return "on_fail 必须是对象"
    mapping = cast(dict[str, Any], value)
    unknown = set(mapping) - {"action", "max_rounds", "note"}
    if unknown:
        return f"on_fail 包含未知字段：{', '.join(sorted(unknown))}"
    action = mapping.get("action")
    if action not in ACTIONS:
        return "on_fail.action 只能是 ESCALATE、RERUN 或 STOP"
    rounds = mapping.get("max_rounds")
    if action == "RERUN":
        if rounds is None:
            return None
        if isinstance(rounds, bool) or not isinstance(rounds, int):
            return "on_fail.max_rounds 必须是整数"
        if not 1 <= rounds <= MAX_ROUNDS_LIMIT:
            return f"on_fail.max_rounds 必须在 1 到 {MAX_ROUNDS_LIMIT} 之间"
    elif rounds is not None:
        return "on_fail.max_rounds 仅在 action=RERUN 时有效"
    note = mapping.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        return "on_fail.note 必须是不超过 500 字的文本"
    return None


def parse_on_fail(policy: dict[str, Any]) -> FailurePolicy | None:
    """Read a frozen gate policy's declaration; invalid or absent means no declaration."""

    config = policy.get("config")
    raw = config.get("on_fail") if isinstance(config, dict) else None
    if not isinstance(raw, dict) or validate_on_fail(raw) is not None:
        return None
    action = cast(OnFailAction, raw["action"])
    rounds = raw.get("max_rounds")
    return FailurePolicy(
        action=action,
        max_rounds=int(rounds) if isinstance(rounds, int) else DEFAULT_ROUNDS,
        note=str(raw.get("note") or ""),
        policy_id=str(policy.get("id") or ""),
    )


# STOP outranks RERUN outranks ESCALATE: a designer who says "end it" must not be
# overridden by another gate on the same node that merely asks for a rerun.
_SEVERITY = {"STOP": 2, "RERUN": 1, "ESCALATE": 0}


def resolve(policies: list[dict[str, Any]], failed_policy_ids: list[str]) -> FailurePolicy | None:
    """Pick the declaration that governs this failure, or ``None`` for the default."""

    declared = [
        parsed
        for policy in policies
        if str(policy.get("id")) in set(failed_policy_ids)
        if (parsed := parse_on_fail(policy)) is not None
    ]
    if not declared:
        return None
    return max(
        declared,
        key=lambda item: (
            _SEVERITY[item.action],
            -item.max_rounds if item.action == "RERUN" else 0,
        ),
    )
