"""Stable public facade for gate execution."""

from flowweave.modules.gates.application.executor import (
    GateExecutionPlan,
    GateResult,
    execute_gate,
    execute_gate_plan,
    prepare_gate,
)
from flowweave.modules.gates.application.failure_policy import resolve as resolve_failure_policy

__all__ = (
    "GateExecutionPlan",
    "GateResult",
    "execute_gate",
    "execute_gate_plan",
    "prepare_gate",
    "resolve_failure_policy",
)
