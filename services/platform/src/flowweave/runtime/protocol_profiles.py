"""Static FlowRun protocol routing for frozen OpenHands Runtime images.

``FlowRunProtocolProfile`` selects compiled adapter code, not an image tag or
source commit. The exact OpenHands baseline remains the immutable provenance
of the Environment Version selected by the FlowRun.

Adding a profile is additive: register a new adapter and migration-backed
allow-list entries for Environment Version, Runtime Session, and Runtime
Generation. Never change an existing adapter or published profile record.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol

from flowweave.runtime.base import (
    FLOWWEAVE_LLM_TIMEOUT_POLICY,
    FLOWWEAVE_SUBAGENT_POLICY,
    RuntimeLLMTimeoutPolicy,
    RuntimePort,
    RuntimeSubagentPolicy,
)
from flowweave.shared.errors import DomainError


class FlowRunProtocolProfile(StrEnum):
    """A code-owned OpenHands contract family frozen by a FlowRun Runtime."""

    OH_FW_V1 = "OH_FW_V1"
    OH_FW_V2 = "OH_FW_V2"


DEFAULT_FLOW_RUN_PROTOCOL_PROFILE: Final = FlowRunProtocolProfile.OH_FW_V1


class FlowRunRuntimeAdapter(Protocol):
    """Binds one frozen FlowRun contract family to the runtime implementation."""

    profile: FlowRunProtocolProfile

    def bind(self, runtime: RuntimePort) -> RuntimePort: ...


class OpenHandsV1FlowRunRuntimeAdapter:
    """The current FlowWeave/OpenHands contract family.

    V1 deliberately delegates to the existing RuntimePort unchanged. Future
    baselines add another compiled adapter here instead of changing V1 calls.
    """

    profile = FlowRunProtocolProfile.OH_FW_V1

    def bind(self, runtime: RuntimePort) -> RuntimePort:
        return runtime


class OpenHandsV2FlowRunRuntimeAdapter:
    """The additive OpenHands 1.54 contract family.

    V2 is selected only by a new, frozen Environment Version. It deliberately
    keeps the RuntimePort object intact while later V2 slices add their native
    route behavior here. This prevents the new contract family from
    reinterpreting V1 Runtime Sessions or changing their request shape.
    """

    profile = FlowRunProtocolProfile.OH_FW_V2

    def bind(self, runtime: RuntimePort) -> RuntimePort:
        return runtime


FLOW_RUN_RUNTIME_ADAPTERS: Final = MappingProxyType(
    {
        FlowRunProtocolProfile.OH_FW_V1: OpenHandsV1FlowRunRuntimeAdapter(),
        FlowRunProtocolProfile.OH_FW_V2: OpenHandsV2FlowRunRuntimeAdapter(),
    }
)


def require_flow_run_protocol_profile(
    profile: FlowRunProtocolProfile | str,
) -> FlowRunProtocolProfile:
    """Return a supported persisted profile without a latest-version fallback."""

    try:
        normalized = FlowRunProtocolProfile(profile)
    except (TypeError, ValueError) as error:
        raise DomainError(
            "RUNTIME_PROTOCOL_PROFILE_UNSUPPORTED",
            "The FlowRun Runtime protocol profile is unsupported",
            409,
            {"protocol_profile": str(profile)},
        ) from error
    if normalized not in FLOW_RUN_RUNTIME_ADAPTERS:
        raise DomainError(
            "RUNTIME_PROTOCOL_PROFILE_UNSUPPORTED",
            "The FlowRun Runtime protocol profile is unsupported",
            409,
            {"protocol_profile": normalized.value},
        )
    return normalized


def runtime_for_flow_run_protocol_profile(
    profile: FlowRunProtocolProfile | str,
    runtime: RuntimePort,
) -> RuntimePort:
    """Resolve a persisted FlowRun profile through the static adapter registry."""

    normalized = require_flow_run_protocol_profile(profile)
    return FLOW_RUN_RUNTIME_ADAPTERS[normalized].bind(runtime)


def llm_timeout_policy_for_flow_run_protocol_profile(
    profile: FlowRunProtocolProfile | str,
) -> RuntimeLLMTimeoutPolicy | None:
    """Return the additive V2-only native timeout policy for one FlowRun."""

    if require_flow_run_protocol_profile(profile) is FlowRunProtocolProfile.OH_FW_V2:
        return FLOWWEAVE_LLM_TIMEOUT_POLICY
    return None


def subagent_policy_for_flow_run_protocol_profile(
    profile: FlowRunProtocolProfile | str,
) -> RuntimeSubagentPolicy | None:
    """Return the additive V2-only native delegation boundary."""

    if require_flow_run_protocol_profile(profile) is FlowRunProtocolProfile.OH_FW_V2:
        return FLOWWEAVE_SUBAGENT_POLICY
    return None


__all__ = (
    "DEFAULT_FLOW_RUN_PROTOCOL_PROFILE",
    "FLOW_RUN_RUNTIME_ADAPTERS",
    "FlowRunProtocolProfile",
    "FlowRunRuntimeAdapter",
    "OpenHandsV1FlowRunRuntimeAdapter",
    "OpenHandsV2FlowRunRuntimeAdapter",
    "llm_timeout_policy_for_flow_run_protocol_profile",
    "require_flow_run_protocol_profile",
    "runtime_for_flow_run_protocol_profile",
    "subagent_policy_for_flow_run_protocol_profile",
)
