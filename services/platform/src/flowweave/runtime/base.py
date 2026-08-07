from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


def _empty_outputs() -> dict[str, tuple[str, str]]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeProvider:
    provider_id: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeSkill:
    name: str
    content: str
    description: str = ""
    source: str = ""
    workspace_path: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeMCP:
    name: str
    config: dict[str, Any]
    workspace_path: str = ""


@dataclass(frozen=True, slots=True)
class StartAttemptRequest:
    attempt_id: str
    execution_key: str
    node: dict[str, Any]
    bindings: list[dict[str, Any]]
    workspace_ref: str
    node_workspace_ref: str = ""
    provider: RuntimeProvider | None = None
    skills: tuple[RuntimeSkill, ...] = ()
    mcp_servers: tuple[RuntimeMCP, ...] = ()
    interaction_mode: Literal["EXECUTION", "COLLABORATION"] = "EXECUTION"


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    job_id: str
    conversation_id: str
    cursor: str | None = None


RuntimeEventType = Literal[
    "MESSAGE",
    "TOOL",
    "TOOL_CALL",
    "TOOL_RESULT",
    "THOUGHT",
    "STATE",
    "OUTPUT",
    "ERROR",
    "HUMAN_INPUT_REQUIRED",
    "COMPLETED",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    cursor: str
    event_type: RuntimeEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeEventBatch:
    events: tuple[RuntimeEvent, ...] = ()
    cursor: str | None = None
    result: RuntimeResult | None = None


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    status: str
    outputs: dict[str, tuple[str, str]] = field(default_factory=_empty_outputs)
    human_question: str | None = None
    cursor: str | None = None
    error: str | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "outputs": {key: list(value) for key, value in self.outputs.items()},
            "human_question": self.human_question,
            "cursor": self.cursor,
            "error": self.error,
        }


class RuntimePort(Protocol):
    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle: ...

    def start(self, request: StartAttemptRequest) -> RuntimeHandle: ...

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch: ...

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult: ...

    def send_message(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult: ...

    def resume(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult: ...

    def cancel(self, handle: RuntimeHandle) -> None: ...
