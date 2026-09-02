from enum import StrEnum


class Direction(StrEnum):
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"


class CapabilityType(StrEnum):
    SKILL = "SKILL"
    PLUGIN = "PLUGIN"
    MCP = "MCP"
    HOOK = "HOOK"
    AGENT_DEFINITION = "AGENT_DEFINITION"
    CONTEXT = "CONTEXT"
    CONTEXT_POLICY = "CONTEXT_POLICY"
    MEMORY_POLICY = "MEMORY_POLICY"
    CRITIC_POLICY = "CRITIC_POLICY"
    AGENT_PROFILE = "AGENT_PROFILE"


class GateStage(StrEnum):
    START = "START"
    END = "END"


class GateType(StrEnum):
    PROMPT = "PROMPT"
    PYTHON = "PYTHON"
    JAVASCRIPT = "JAVASCRIPT"


class FlowRunState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeRunState(StrEnum):
    ACTIVE = "ACTIVE"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    WAITING_INPUT = "WAITING_INPUT"
    START_GATES = "START_GATES"
    START_BLOCKED = "START_BLOCKED"
    WAITING_START_CONFIRMATION = "WAITING_START_CONFIRMATION"
    EXECUTING = "EXECUTING"
    # A user-paused native conversation is a stable orchestration wait state.
    # It is intentionally distinct from WAITING_HUMAN: the latter means the
    # Agent requested information, while PAUSED is an explicit operator stop.
    PAUSED = "PAUSED"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    END_GATES = "END_GATES"
    END_BLOCKED = "END_BLOCKED"
    WAITING_ACCEPTANCE = "WAITING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY = "RETRY"
    DEAD = "DEAD"
