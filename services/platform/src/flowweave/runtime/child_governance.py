"""V4 wire projections; deployment authority is never inferred from these DTOs."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from flowweave.runtime.child_creation import (
    NativeChildIdentity,
    NativeChildInvocation,
    NativeChildPreparation,
)


class NativeChildResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    invocation: NativeChildInvocation
    identity: NativeChildIdentity

    @model_validator(mode="after")
    def validate_resolution(self) -> NativeChildResolution:
        invocation = self.invocation
        parent = invocation.policy.parent_conversation_id
        if self.identity.parent_conversation_id != parent or self.identity.creation_id != uuid5(
            parent,
            f"openhands-child-invocation-v1:{invocation.action_id}:{invocation.tool_call_id}",
        ):
            raise ValueError("Native launcher resolution identity mismatch")
        return self


class NativeChildStaticSecret(BaseModel):
    """FlowWeave only needs native StaticSecret sources for binding credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["StaticSecret"] = "StaticSecret"
    value: SecretStr = Field(repr=False)
    description: str | None = None

    def wire_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value.get_secret_value(),
            **({"description": self.description} if self.description is not None else {}),
        }


class NativeChildPreparationOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    governance_id: UUID
    remove_secrets: frozenset[str] = frozenset()
    replace_secrets: dict[str, NativeChildStaticSecret] = Field(default_factory=dict, repr=False)
    remove_mcp_servers: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_names(self) -> NativeChildPreparationOverrides:
        names = self.remove_secrets | self.replace_secrets.keys() | self.remove_mcp_servers
        if any(not name or len(name) > 200 for name in names):
            raise ValueError("Invalid native child governance name")
        if self.remove_secrets & self.replace_secrets.keys():
            raise ValueError("A child secret cannot be removed and replaced")
        return self

    def wire_payload(self) -> dict[str, Any]:
        return {
            "governance_id": str(self.governance_id),
            "remove_secrets": sorted(self.remove_secrets),
            "replace_secrets": {
                name: source.wire_payload() for name, source in self.replace_secrets.items()
            },
            "remove_mcp_servers": sorted(self.remove_mcp_servers),
        }


class NativeGovernedChildPreparation(NativeChildPreparation):
    governance_id: UUID | None = None
