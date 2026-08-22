from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.base import (
    RuntimeAgentSpec,
    RuntimeContract,
    RuntimeProvider,
    RuntimeTool,
    StartAttemptRequest,
)
from flowweave.runtime.contract import (
    compile_runtime_contract,
    governed_runtime_contract,
    normalize_runtime_contract,
)
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.domain.tool_policy import OPENHANDS_SOURCE_COMMIT, OPENHANDS_VERSION
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database():
    """Contract refusal checks do not require PostgreSQL."""

    yield


def _server_info(*, tools: tuple[str, ...]) -> dict[str, object]:
    return {
        "version": OPENHANDS_VERSION,
        "sdk_version": OPENHANDS_VERSION,
        "tools_version": OPENHANDS_VERSION,
        "workspace_version": OPENHANDS_VERSION,
        "build_git_sha": OPENHANDS_SOURCE_COMMIT,
        "build_git_ref": OPENHANDS_SOURCE_COMMIT,
        "capabilities": [
            "credential_binding_v1",
            "credential_binding_readiness_probe_v1",
            "credential_binding_activation_guard_v1",
        ],
        "usable_tools": list(tools),
    }


def _openapi(contract: RuntimeContract) -> dict[str, Any]:
    paths: dict[str, dict[str, object]] = {}
    for method, path in contract.required_http_operations:
        paths.setdefault(path, {})[method.lower()] = {}
    paths["/api/conversations"]["post"] = {
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/StartConversationRequest"}
                }
            }
        }
    }
    return {
        "paths": paths,
        "components": {
            "schemas": {
                "StartConversationRequest": {
                    "properties": {field: {} for field in contract.required_start_fields}
                }
            }
        },
    }


def _reason(error: pytest.ExceptionInfo[DomainError]) -> object:
    return error.value.details.get("reason")


def test_runtime_contract_rejects_missing_or_mutated_snapshot_contract() -> None:
    tools = ("file_editor", "terminal")
    with pytest.raises(ValueError, match="must be an object"):
        normalize_runtime_contract(None, required_tools=tools)

    document = compile_runtime_contract(tools)
    document["source_commit"] = "0" * 40
    with pytest.raises(ValueError, match="does not match"):
        normalize_runtime_contract(document, required_tools=tools)


def test_start_rejects_missing_contract_before_runtime_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = OpenHandsRuntime(
        Settings(
            runtime_adapter="openhands",
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            sandbox_manager_scope="flowweave-contract-test",
        )
    )
    requests: list[tuple[str, str]] = []

    def unexpected_request(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        requests.append((method, path))
        raise AssertionError("Runtime HTTP must not run before contract acceptance")

    monkeypatch.setattr(runtime, "_request", unexpected_request)
    request = StartAttemptRequest(
        attempt_id="attempt-contract-missing",
        execution_key="attempt:contract-missing:start",
        node={"instance_key": "node", "asset": {"name": "Contract test"}},
        bindings=[],
        workspace_ref=str(tmp_path / "workspace"),
        agent_spec=RuntimeAgentSpec(
            provider=RuntimeProvider(
                provider_id="provider",
                base_url="https://provider.example/v1",
                model="model",
                api_key="secret",
            ),
            tools=(RuntimeTool(name="terminal"),),
        ),
    )

    with pytest.raises(DomainError) as error:
        runtime.start(request)

    assert error.value.code == "RUNTIME_CONTRACT_INCOMPATIBLE"
    assert _reason(error) == "snapshot_contract_missing"
    assert requests == []


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("source", "source_commit_mismatch"),
        ("route", "missing_http_operations"),
        ("field", "missing_start_conversation_fields"),
        ("tool", "missing_capabilities"),
    ],
)
def test_runtime_contract_rejects_incompatible_server(mutation: str, expected_reason: str) -> None:
    tools = ("file_editor", "terminal")
    contract = governed_runtime_contract(tools)
    server_info = _server_info(tools=tools)
    openapi = _openapi(contract)
    if mutation == "source":
        server_info["build_git_sha"] = "0" * 40
    elif mutation == "route":
        paths = cast(dict[str, Any], openapi["paths"])
        paths.pop("/api/conversations/{conversation_id}/ask_agent")
    elif mutation == "field":
        components = cast(dict[str, Any], openapi["components"])
        schemas = cast(dict[str, Any], components["schemas"])
        start = cast(dict[str, Any], schemas["StartConversationRequest"])
        properties = cast(dict[str, Any], start["properties"])
        properties.pop("plugins")
    else:
        server_info["usable_tools"] = ["terminal"]

    with pytest.raises(DomainError) as error:
        OpenHandsRuntime._validate_runtime_contract(  # pyright: ignore[reportPrivateUsage]
            contract,
            ready={"status": "ready"},
            server_info=deepcopy(server_info),
            openapi=deepcopy(openapi),
        )
    assert error.value.code == "RUNTIME_CONTRACT_INCOMPATIBLE"
    assert _reason(error) == expected_reason


def test_runtime_contract_accepts_openapi_schema_annotations() -> None:
    """FastAPI may annotate a valid request $ref with examples or metadata."""

    tools = ("file_editor", "terminal")
    contract = governed_runtime_contract(tools)
    openapi = _openapi(contract)
    paths = cast(dict[str, Any], openapi["paths"])
    operation = cast(dict[str, Any], paths["/api/conversations"]["post"])
    request_schema = cast(
        dict[str, Any],
        operation["requestBody"]["content"]["application/json"]["schema"],
    )
    request_schema["examples"] = [{"initial_message": {"content": [{"text": "Hello"}]}}]

    OpenHandsRuntime._validate_runtime_contract(  # pyright: ignore[reportPrivateUsage]
        contract,
        ready={"status": "ready"},
        server_info=_server_info(tools=tools),
        openapi=openapi,
    )
