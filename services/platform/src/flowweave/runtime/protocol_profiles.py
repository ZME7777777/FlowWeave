"""Static FlowRun protocol routing for frozen OpenHands Runtime images.

``FlowRunProtocolProfile`` selects compiled adapter code, not an image tag or
source commit. The exact OpenHands baseline remains the immutable provenance
of the Environment Version selected by the FlowRun.

Adding a profile is additive: register a new adapter and migration-backed
allow-list entries for Environment Version, Runtime Session, and Runtime
Generation. Never change an existing adapter or published profile record.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol

from flowweave.runtime.base import (
    FLOWWEAVE_LLM_TIMEOUT_POLICY,
    FLOWWEAVE_SUBAGENT_POLICY,
    RuntimeContract,
    RuntimeLLMTimeoutPolicy,
    RuntimePort,
    RuntimeSubagentPolicy,
)
from flowweave.runtime.child_contract import (
    CHILD_HTTP_OPERATIONS,
    V3_PACKAGE_VERSION,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
)
from flowweave.runtime.child_governance_contract import (
    V4_CHILD_HTTP_OPERATIONS,
    V4_PACKAGE_VERSION,
    V4_SOURCE_COMMIT,
    V5_PACKAGE_VERSION,
    V5_SOURCE_COMMIT,
    governed_child_capability_manifest,
)
from flowweave.runtime.contract import runtime_contract_for_server_identity
from flowweave.shared.domain.openhands import OpenHandsServerIdentity
from flowweave.shared.errors import DomainError


class FlowRunProtocolProfile(StrEnum):
    """A code-owned OpenHands contract family frozen by a FlowRun Runtime."""

    OH_FW_V1 = "OH_FW_V1"
    OH_FW_V2 = "OH_FW_V2"
    OH_FW_V3 = "OH_FW_V3"
    OH_FW_V4 = "OH_FW_V4"
    OH_FW_V5 = "OH_FW_V5"


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


class OpenHandsV3FlowRunRuntimeAdapter:
    """Shared native children; hosts attach tools only after binding governance."""

    profile = FlowRunProtocolProfile.OH_FW_V3

    def bind(self, runtime: RuntimePort) -> RuntimePort:
        return runtime

    def contract(
        self, required_tools: tuple[str, ...], identity: OpenHandsServerIdentity
    ) -> RuntimeContract:
        if (
            identity.source_commit != V3_SOURCE_COMMIT
            or identity.package_version != V3_PACKAGE_VERSION
        ):
            raise DomainError(
                "RUNTIME_CHILD_CAPABILITY_INCOMPATIBLE",
                "V3 requires its audited native child extension",
                409,
            )
        base = runtime_contract_for_server_identity(required_tools, identity)
        return replace(
            base,
            schema_version=6,
            required_http_operations=tuple(
                sorted(set(base.required_http_operations) | set(CHILD_HTTP_OPERATIONS))
            ),
        )


class OpenHandsV4FlowRunRuntimeAdapter:
    """Native creation resolution and immutable prepare-time credential overrides."""

    profile = FlowRunProtocolProfile.OH_FW_V4

    def bind(self, runtime: RuntimePort) -> RuntimePort:
        return runtime

    def contract(
        self, required_tools: tuple[str, ...], identity: OpenHandsServerIdentity
    ) -> RuntimeContract:
        if (
            identity.source_commit != V4_SOURCE_COMMIT
            or identity.source_ref != V4_SOURCE_COMMIT
            or identity.package_version != V4_PACKAGE_VERSION
        ):
            raise DomainError(
                "RUNTIME_CHILD_CAPABILITY_INCOMPATIBLE",
                "V4 requires its audited native child governance extension",
                409,
            )
        base = runtime_contract_for_server_identity(required_tools, identity)
        return replace(
            base,
            schema_version=7,
            required_http_operations=tuple(
                sorted(set(base.required_http_operations) | set(V4_CHILD_HTTP_OPERATIONS))
            ),
        )


class OpenHandsV5FlowRunRuntimeAdapter:
    """Native creation resolution and immutable prepare-time credential overrides."""

    profile = FlowRunProtocolProfile.OH_FW_V5

    def bind(self, runtime: RuntimePort) -> RuntimePort:
        return runtime

    def contract(
        self, required_tools: tuple[str, ...], identity: OpenHandsServerIdentity
    ) -> RuntimeContract:
        if (
            identity.source_commit != V5_SOURCE_COMMIT
            or identity.source_ref != V5_SOURCE_COMMIT
            or identity.package_version != V5_PACKAGE_VERSION
        ):
            raise DomainError(
                "RUNTIME_CHILD_CAPABILITY_INCOMPATIBLE",
                "V5 requires its audited native child governance extension",
                409,
            )
        base = runtime_contract_for_server_identity(required_tools, identity)
        return replace(
            base,
            schema_version=8,
            required_http_operations=tuple(
                sorted(set(base.required_http_operations) | set(V4_CHILD_HTTP_OPERATIONS))
            ),
        )


FLOW_RUN_RUNTIME_ADAPTERS: Final = MappingProxyType(
    {
        FlowRunProtocolProfile.OH_FW_V1: OpenHandsV1FlowRunRuntimeAdapter(),
        FlowRunProtocolProfile.OH_FW_V2: OpenHandsV2FlowRunRuntimeAdapter(),
        FlowRunProtocolProfile.OH_FW_V3: OpenHandsV3FlowRunRuntimeAdapter(),
        FlowRunProtocolProfile.OH_FW_V4: OpenHandsV4FlowRunRuntimeAdapter(),
        FlowRunProtocolProfile.OH_FW_V5: OpenHandsV5FlowRunRuntimeAdapter(),
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
    """Return the native V2/V3 timeout policy for one FlowRun."""

    if require_flow_run_protocol_profile(profile) in {
        FlowRunProtocolProfile.OH_FW_V2,
        FlowRunProtocolProfile.OH_FW_V3,
        FlowRunProtocolProfile.OH_FW_V4,
        FlowRunProtocolProfile.OH_FW_V5,
    }:
        return FLOWWEAVE_LLM_TIMEOUT_POLICY
    return None


def subagent_policy_for_flow_run_protocol_profile(
    profile: FlowRunProtocolProfile | str,
) -> RuntimeSubagentPolicy | None:
    """Return the native V2/V3 delegation boundary."""

    if require_flow_run_protocol_profile(profile) in {
        FlowRunProtocolProfile.OH_FW_V2,
        FlowRunProtocolProfile.OH_FW_V3,
        FlowRunProtocolProfile.OH_FW_V4,
        FlowRunProtocolProfile.OH_FW_V5,
    }:
        return FLOWWEAVE_SUBAGENT_POLICY
    return None


def runtime_contract_for_protocol_profile(
    profile: FlowRunProtocolProfile | str,
    required_tools: tuple[str, ...],
    identity: OpenHandsServerIdentity,
) -> RuntimeContract:
    normalized = require_flow_run_protocol_profile(profile)
    if normalized is FlowRunProtocolProfile.OH_FW_V5:
        return OpenHandsV5FlowRunRuntimeAdapter().contract(required_tools, identity)
    if normalized is FlowRunProtocolProfile.OH_FW_V4:
        return OpenHandsV4FlowRunRuntimeAdapter().contract(required_tools, identity)
    if normalized is FlowRunProtocolProfile.OH_FW_V3:
        return OpenHandsV3FlowRunRuntimeAdapter().contract(required_tools, identity)
    return runtime_contract_for_server_identity(required_tools, identity)


def child_manifest_for_protocol_profile(
    profile: FlowRunProtocolProfile | str,
) -> dict[str, Any] | None:
    normalized = require_flow_run_protocol_profile(profile)
    if normalized in {FlowRunProtocolProfile.OH_FW_V4, FlowRunProtocolProfile.OH_FW_V5}:
        return governed_child_capability_manifest(normalized.value)
    if normalized is FlowRunProtocolProfile.OH_FW_V3:
        return child_capability_manifest()
    return None


__all__ = (
    "DEFAULT_FLOW_RUN_PROTOCOL_PROFILE",
    "FLOW_RUN_RUNTIME_ADAPTERS",
    "FlowRunProtocolProfile",
    "FlowRunRuntimeAdapter",
    "OpenHandsV1FlowRunRuntimeAdapter",
    "OpenHandsV2FlowRunRuntimeAdapter",
    "OpenHandsV3FlowRunRuntimeAdapter",
    "OpenHandsV4FlowRunRuntimeAdapter",
    "OpenHandsV5FlowRunRuntimeAdapter",
    "runtime_contract_for_protocol_profile",
    "child_manifest_for_protocol_profile",
    "llm_timeout_policy_for_flow_run_protocol_profile",
    "require_flow_run_protocol_profile",
    "runtime_for_flow_run_protocol_profile",
    "subagent_policy_for_flow_run_protocol_profile",
)
