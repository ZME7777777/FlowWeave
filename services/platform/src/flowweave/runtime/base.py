from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


def _empty_outputs() -> dict[str, tuple[str, str]]:
    return {}


def _empty_output_targets() -> dict[str, dict[str, str]]:
    return {}


def _empty_conversation_history() -> tuple[dict[str, str], ...]:
    return ()


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
    dependency_runtime_path: str = ""


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
    startup_prompt: str | None = None
    startup_capability_key: str | None = None
    conversation_history: tuple[dict[str, str], ...] = field(
        default_factory=_empty_conversation_history
    )
    delegation_enabled: bool = False
    output_targets: dict[str, dict[str, str]] = field(default_factory=_empty_output_targets)
    environment_image: str = ""
    environment_id: str = ""
    environment_version_id: str = ""
    environment_version_no: int = 0
    runtime_workspace_relative: str = ""
    runtime_sandbox_id: str = ""
    runtime_resource_name: str = ""
    runtime_base_url: str = ""


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
    final_message: str | None = None
    human_question: str | None = None
    cursor: str | None = None
    error: str | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "outputs": {key: list(value) for key, value in self.outputs.items()},
            "final_message": self.final_message,
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
