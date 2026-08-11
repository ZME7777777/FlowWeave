from enum import StrEnum


class ConversationKind(StrEnum):
    AUTO = "AUTO"
    HUMAN_CREATED = "HUMAN_CREATED"
    SUBAGENT = "SUBAGENT"


class ConversationState(StrEnum):
    CREATING = "CREATING"
    IDLE = "IDLE"
    GENERATING = "GENERATING"
    STOPPING = "STOPPING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_SUBAGENTS = "WAITING_SUBAGENTS"
    FAILED = "FAILED"
    READ_ONLY = "READ_ONLY"


class MessageSource(StrEnum):
    PROGRAM = "PROGRAM"
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class MessageType(StrEnum):
    TEXT = "TEXT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    STATE = "STATE"
    ERROR = "ERROR"


class DeliveryState(StrEnum):
    QUEUED = "QUEUED"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DeliveryMode(StrEnum):
    QUEUE_AFTER_TURN = "QUEUE_AFTER_TURN"
    INTERRUPT_AND_RESUME = "INTERRUPT_AND_RESUME"


TERMINAL_ATTEMPT_STATES = {"ACCEPTED", "REJECTED", "CANCELLED"}
CONVERSATION_ENABLED_ATTEMPT_STATES = {
    "WAITING_START_CONFIRMATION",
    "EXECUTING",
    "WAITING_HUMAN",
    "END_GATES",
    "END_BLOCKED",
    "WAITING_ACCEPTANCE",
}


def transport_role(source: str) -> str:
    return "assistant" if source == MessageSource.AGENT else "user"
