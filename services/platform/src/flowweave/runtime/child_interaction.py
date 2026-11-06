"""Fixed V4 native child query/command wire types, not execution logic."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from flowweave.runtime.child_creation import NativeChildPolicy


class NativeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["list", "read", "wait"]
    conversation_id: UUID | None = None
    cursor: str | None = Field(default=None, max_length=16384)
    mode: Literal["events", "results"] = "events"
    limit: int = Field(default=20, ge=1, le=50)
    max_chars: int = Field(default=12000, ge=1, le=12000)
    checkpoint: str | None = Field(default=None, max_length=4096)
    timeout_seconds: float = Field(default=30, ge=0, le=30)

    @model_validator(mode="after")
    def check_operation(self) -> Self:
        if (
            (self.operation == "list") != (self.conversation_id is None)
            or self.operation == "wait"
            and self.cursor is not None
            or self.operation != "wait"
            and self.checkpoint is not None
        ):
            raise ValueError("Invalid native query")
        return self


class NativeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["send", "interrupt"]
    conversation_id: UUID
    text: str | None = Field(default=None, min_length=1, max_length=100000, repr=False)

    @model_validator(mode="after")
    def check_operation(self) -> Self:
        if (self.operation == "send") != (self.text is not None):
            raise ValueError("Invalid native command")
        return self


class NativeQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: NativeQuery
    token: SecretStr = Field(repr=False)

    def wire_payload(self) -> dict[str, object]:
        return {"query": self.query.model_dump(mode="json"), "token": self.token.get_secret_value()}


class NativeCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command: NativeCommand
    token: SecretStr = Field(repr=False)

    def wire_payload(self) -> dict[str, object]:
        return {
            "command": self.command.model_dump(mode="json"),
            "token": self.token.get_secret_value(),
        }


class NativeQueryInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    purpose: Literal["child-query"]
    policy: NativeChildPolicy
    action_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    audience: str
    query: NativeQuery
    issued_at: datetime
    expires_at: datetime


class NativeCommandInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    purpose: Literal["child-command"]
    policy: NativeChildPolicy
    action_id: str
    tool_call_id: str
    audience: str
    command: NativeCommand
    issued_at: datetime
    expires_at: datetime


class NativeCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    operation: Literal["send", "interrupt"]
    command_id: UUID
    event_id: str | None = None
    replayed: bool = False
    interrupted: bool = False
    pending: bool = False


class NativeChildSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    parent_conversation_id: UUID
    title: str | None = None
    title_truncated: bool = False
    execution_status: Literal[
        "idle", "running", "paused", "finished", "error", "stuck", "waiting_for_confirmation"
    ]
    preparation_phase: Literal["prepared", "dispatching", "started"]
    leaf_event_id: str | None = None
    checkpoint: str


class NativeExcerpt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    parent_id: str | None
    kind: str
    source: str
    tool_name: str | None = None
    action_id: str | None = None
    tool_call_id: str | None = None
    text: str | None = None
    text_offset: int = 0
    next_text_offset: int | None = None
    complete: bool = True


class NativeListPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["list"]
    items: list[NativeChildSnapshot] = Field(max_length=50)
    next_cursor: str | None = None
    complete: bool


class NativeReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["read"]
    snapshot: NativeChildSnapshot
    items: list[NativeExcerpt] = Field(max_length=50)
    next_cursor: str | None = None
    complete: bool


class NativeWaitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["wait"]
    snapshot: NativeChildSnapshot
    changed: bool
    timed_out: bool


NativeQueryResult = NativeListPage | NativeReadPage | NativeWaitResult


class NativeChildInstructionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["start", "send", "interrupt"]
    parent_conversation_id: UUID
    action_id: str
    tool_call_id: str
    scope_id: UUID
    runtime_session_id: UUID
    generation: int
    manifest_digest: str
