"""Typed projections of the fixed native child HTTP contract.

These values contain runtime evidence, never platform user authorization.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from flowweave.shared.errors import DomainError


class NativeChildLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=1, repr=False)
    title: str | None = None
    isolation: Literal["shared"] = "shared"
    token: SecretStr = Field(repr=False)

    def verifier_payload(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "title": self.title,
            "isolation": self.isolation,
            "token": self.token.get_secret_value(),
        }

    def preparation_payload(self) -> dict[str, Any]:
        value = self.verifier_payload()
        value["launcher_token"] = value.pop("token")
        return value

    def request_digest(self) -> str:
        """The fixed native wire digest; never an execution or authorization decision."""
        encoded = json.dumps(
            {"task": self.task, "title": self.title, "isolation": self.isolation},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class NativeChildPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_conversation_id: UUID
    scope_id: UUID
    runtime_session_id: UUID
    generation: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_active_children: int = Field(ge=1, le=3)


class NativeChildInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    operation: Literal["start"]
    policy: NativeChildPolicy
    action_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime


class NativeChildIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_conversation_id: UUID
    creation_id: UUID
    conversation_id: UUID

    @model_validator(mode="after")
    def validate_native_identity(self) -> NativeChildIdentity:
        expected = uuid5(
            self.parent_conversation_id,
            f"openhands-child-creation-v1:{self.creation_id.hex}",
        )
        if self.conversation_id != expected:
            raise ValueError("Invalid native creation identity")
        return self


class NativeChildReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    identity: NativeChildIdentity
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class NativeChildPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: NativeChildReceipt
    phase: Literal["reserved", "prepared", "dispatching", "started"]
    workspace: str | None = None


class NativeChildWorkspace(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    working_dir: str


class NativeChildConfiguration(BaseModel):
    """Formal public metadata; excluded from repr because config may be sensitive."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    parent_conversation_id: UUID
    workspace: NativeChildWorkspace
    persistence_dir: str | None
    agent: dict[str, Any] = Field(repr=False)


def child_contract_error(*, outcome_unknown: bool = False) -> DomainError:
    return DomainError(
        "RUNTIME_CHILD_PROTOCOL_INVALID",
        "OpenHands 子会话响应不符合冻结契约",
        502,
        {"outcome_unknown": outcome_unknown},
    )


def verify_child_configuration(
    actual: NativeChildConfiguration,
    *,
    expected: NativeChildIdentity,
    working_directory: str,
    expected_agent: dict[str, Any],
) -> None:
    """Compare the native public Agent document frozen during preparation.

    Both Agent documents must come from the same native serialization boundary.
    The control plane does not resolve plugins or reconstruct native executors.
    """

    if (
        actual.id != expected.conversation_id
        or actual.parent_conversation_id != expected.parent_conversation_id
        or actual.workspace.working_dir != working_directory
        or actual.agent != expected_agent
    ):
        raise DomainError(
            "AGENT_CHILD_NATIVE_IDENTITY_MISMATCH", "原生子会话身份或冻结配置不匹配", 409
        )
