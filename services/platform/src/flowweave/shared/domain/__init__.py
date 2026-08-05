"""Framework-free shared domain primitives."""

from flowweave.shared.domain.enums import (
    AttemptState,
    CapabilityType,
    Direction,
    FlowRunState,
    GateStage,
    GateType,
    NodeRunState,
    TaskState,
)
from flowweave.shared.domain.errors import DomainError, conflict, illegal, not_found

__all__ = (
    "AttemptState",
    "CapabilityType",
    "Direction",
    "DomainError",
    "FlowRunState",
    "GateStage",
    "GateType",
    "NodeRunState",
    "TaskState",
    "conflict",
    "illegal",
    "not_found",
)
