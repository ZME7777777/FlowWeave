"""Stable public facade for gate execution."""

from flowweave.modules.gates.application.executor import (
    GateExecutionPlan,
    GateResult,
    execute_gate,
    execute_gate_plan,
    prepare_gate,
)

__all__ = (
    "GateExecutionPlan",
    "GateResult",
    "execute_gate",
    "execute_gate_plan",
    "prepare_gate",
)
