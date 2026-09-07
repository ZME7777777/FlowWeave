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
from flowweave.shared.domain.event_automation import (
    ActionType,
    EventSource,
    EventTrigger,
    FailureClass,
    OBSERVATION_EVENT_TYPES,
    STATE_DRIVING_EVENT_TYPES,
    TriggerAction,
    TriggerEvent,
    TriggerFilter,
    action_idempotency_key,
    classify_failure,
    is_auto_recovery_eligible,
)

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
    "ActionType",
    "EventSource",
    "EventTrigger",
    "FailureClass",
    "OBSERVATION_EVENT_TYPES",
    "STATE_DRIVING_EVENT_TYPES",
    "TriggerAction",
    "TriggerEvent",
    "TriggerFilter",
    "action_idempotency_key",
    "classify_failure",
    "is_auto_recovery_eligible",
)
