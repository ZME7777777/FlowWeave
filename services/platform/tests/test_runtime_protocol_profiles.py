from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.protocol_profiles import (
    DEFAULT_FLOW_RUN_PROTOCOL_PROFILE,
    FLOW_RUN_RUNTIME_ADAPTERS,
    FlowRunProtocolProfile,
    OpenHandsV1FlowRunRuntimeAdapter,
    OpenHandsV2FlowRunRuntimeAdapter,
    runtime_for_flow_run_protocol_profile,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import EnvironmentPublishWrite


def test_v1_profile_binds_the_existing_runtime_port() -> None:
    runtime = MockRuntime()

    resolved = runtime_for_flow_run_protocol_profile(
        DEFAULT_FLOW_RUN_PROTOCOL_PROFILE,
        runtime,
    )

    assert resolved is runtime
    assert DEFAULT_FLOW_RUN_PROTOCOL_PROFILE is FlowRunProtocolProfile.OH_FW_V1
    assert isinstance(
        FLOW_RUN_RUNTIME_ADAPTERS[FlowRunProtocolProfile.OH_FW_V1],
        OpenHandsV1FlowRunRuntimeAdapter,
    )


def test_v2_profile_uses_a_distinct_compiled_adapter_without_reinterpreting_v1() -> None:
    runtime = MockRuntime()

    resolved = runtime_for_flow_run_protocol_profile(FlowRunProtocolProfile.OH_FW_V2, runtime)

    assert resolved is runtime
    assert set(FLOW_RUN_RUNTIME_ADAPTERS) == {
        FlowRunProtocolProfile.OH_FW_V1,
        FlowRunProtocolProfile.OH_FW_V2,
    }
    assert isinstance(
        FLOW_RUN_RUNTIME_ADAPTERS[FlowRunProtocolProfile.OH_FW_V2],
        OpenHandsV2FlowRunRuntimeAdapter,
    )
    assert (
        FLOW_RUN_RUNTIME_ADAPTERS[FlowRunProtocolProfile.OH_FW_V1]
        is not FLOW_RUN_RUNTIME_ADAPTERS[FlowRunProtocolProfile.OH_FW_V2]
    )


def test_environment_publish_request_explicitly_selects_a_compiled_profile() -> None:
    payload = EnvironmentPublishWrite(runtime_protocol_profile="OH_FW_V2")

    assert payload.runtime_protocol_profile is FlowRunProtocolProfile.OH_FW_V2
    with pytest.raises(ValidationError):
        EnvironmentPublishWrite(runtime_protocol_profile="OH_FW_V3")


def test_unknown_profile_fails_closed_without_latest_fallback() -> None:
    with pytest.raises(DomainError) as caught:
        runtime_for_flow_run_protocol_profile("OH_FW_V3", MockRuntime())

    assert caught.value.code == "RUNTIME_PROTOCOL_PROFILE_UNSUPPORTED"
