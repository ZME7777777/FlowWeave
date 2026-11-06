from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application.runtime_config import (
    FrozenSessionConfig,
    build_agent_spec,
)
from flowweave.modules.environments.application.service import validate_runtime_manifest
from flowweave.modules.orchestration.application.service import _compile_runtime_manifest
from flowweave.runtime.base import (
    FLOWWEAVE_LLM_TIMEOUT_POLICY,
    FLOWWEAVE_SUBAGENT_POLICY,
    RuntimeHandle,
)
from flowweave.runtime.child_contract import (
    CHILD_HTTP_OPERATIONS,
    CHILD_TOOL_NAMES,
    V3_ARCHIVE_SHA256,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
    native_child_tool_entries,
    require_child_capability_manifest,
)
from flowweave.runtime.contract import (
    normalize_runtime_contract,
    runtime_contract_document,
    runtime_contract_for_server_identity,
)
from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.runtime.protocol_profiles import (
    child_manifest_for_protocol_profile,
    runtime_contract_for_protocol_profile,
    runtime_for_flow_run_protocol_profile,
)
from flowweave.shared.domain.openhands import OpenHandsServerIdentity
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import EnvironmentPublishWrite
from flowweave.shared.settings import settings_context


def test_v3_agent_spec_refuses_an_unidentified_image_before_materialization(tmp_path: Path):
    config = FrozenSessionConfig(None, None, None, None, ())
    with pytest.raises(DomainError, match="explicitly frozen"):
        build_agent_spec(
            config,
            provider=None,
            binding_id=str(uuid4()),
            working_directory="/workspace",
            host_root=tmp_path / "not-created",
            runtime_root=Path("/runtime/capabilities"),
            protocol_profile="OH_FW_V3",
        )
    assert not (tmp_path / "not-created").exists()


def test_v3_agent_spec_retains_task_scope_without_installing_ungoverned_parent_tools(
    tmp_path: Path,
):
    with settings_context(Settings(workspace_root=tmp_path)):
        spec = build_agent_spec(
            FrozenSessionConfig(None, None, None, None, ()),
            provider=None,
            binding_id=str(uuid4()),
            working_directory="/workspace",
            host_root=tmp_path / "capabilities",
            runtime_root=Path("/runtime/capabilities"),
            runtime_server_identity=OpenHandsServerIdentity(
                "1.58.2", V3_SOURCE_COMMIT, V3_SOURCE_COMMIT
            ),
            protocol_profile="OH_FW_V3",
        )
    assert spec.subagent_policy == FLOWWEAVE_SUBAGENT_POLICY
    assert set(CHILD_TOOL_NAMES).isdisjoint(tool.name for tool in spec.tools)
    assert set(CHILD_HTTP_OPERATIONS) <= set(spec.runtime_contract.required_http_operations)


def test_v3_contract_requires_native_routes_and_frozen_source_without_changing_old_profiles():
    old = OpenHandsServerIdentity("1.54.0", "a" * 40, "a" * 40)
    current = OpenHandsServerIdentity("1.58.2", V3_SOURCE_COMMIT, V3_SOURCE_COMMIT)
    for profile in ("OH_FW_V1", "OH_FW_V2"):
        base = runtime_contract_for_server_identity(("terminal",), old)
        contract = runtime_contract_for_protocol_profile(profile, ("terminal",), old)
        assert contract == base
        assert child_manifest_for_protocol_profile(profile) is None
        document = runtime_contract_document(contract)
        assert normalize_runtime_contract(document, required_tools=("terminal",)) == contract
    with pytest.raises(DomainError, match="audited"):
        runtime_contract_for_protocol_profile("OH_FW_V3", ("terminal",), old)
    contract = runtime_contract_for_protocol_profile("OH_FW_V3", ("terminal",), current)
    definition = {"nodes": [{"instance_key": "node", "asset": {"id": "asset"}}]}
    old_manifest = _compile_runtime_manifest(definition, server_identity=old)
    assert "child_conversations" not in old_manifest
    new_manifest = _compile_runtime_manifest(
        definition, server_identity=current, protocol_profile="OH_FW_V3"
    )
    assert new_manifest["child_conversations"] == child_capability_manifest()
    assert set(CHILD_HTTP_OPERATIONS) <= set(contract.required_http_operations)
    assert contract.required_tools == ("terminal",)  # no ungoverned tool installation
    assert EnvironmentPublishWrite(runtime_protocol_profile="OH_FW_V3").runtime_protocol_profile
    runtime = MockRuntime()
    assert runtime_for_flow_run_protocol_profile("OH_FW_V3", runtime) is runtime
    assert (
        RuntimeHandle(
            job_id="test", conversation_id="test", protocol_profile="OH_FW_V3"
        ).llm_timeout_policy
        == FLOWWEAVE_LLM_TIMEOUT_POLICY
    )

    server = {
        "version": "1.58.2",
        "sdk_version": "1.58.2",
        "tools_version": "1.58.2",
        "workspace_version": "1.58.2",
        "build_git_sha": V3_SOURCE_COMMIT,
        "build_git_ref": V3_SOURCE_COMMIT,
        "capabilities": ["conversation_runtime_routes_v1"],
        "usable_tools": ["terminal"],
    }
    paths = {}
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
    schema = {
        "paths": paths,
        "components": {
            "schemas": {
                "StartConversationRequest": {
                    "properties": {field: {} for field in contract.required_start_fields}
                }
            }
        },
    }
    OpenHandsRuntime._validate_runtime_contract(
        contract, ready={"status": "ready"}, server_info=server, openapi=schema
    )
    paths.pop(CHILD_HTTP_OPERATIONS[0][1])
    with pytest.raises(DomainError) as caught:
        OpenHandsRuntime._validate_runtime_contract(
            contract, ready={"status": "ready"}, server_info=server, openapi=schema
        )
    assert caught.value.details["reason"] == "missing_http_operations"


def test_frozen_child_manifest_and_native_tool_compilation():
    manifest = child_capability_manifest()
    changed = deepcopy(manifest)
    changed["max_active_children"] = 4
    with pytest.raises(DomainError):
        require_child_capability_manifest(changed)
    parent, scope, session = uuid4(), uuid4(), uuid4()
    tools = native_child_tool_entries(
        manifest=manifest,
        parent_conversation_id=parent,
        scope_id=scope,
        runtime_session_id=session,
        generation=2,
        manifest_digest="b" * 64,
        launch_url="https://control.example/launch",
        query_url="https://control.example/query",
        command_url="https://control.example/command",
    )
    assert tuple(tool["name"] for tool in tools) == CHILD_TOOL_NAMES
    assert tools[0]["params"]["launch_url"] == "https://control.example/launch"
    assert tools[1]["params"]["access_url"] == "https://control.example/query"
    assert tools[5]["params"]["access_url"] == "https://control.example/command"
    assert all(
        tool["params"]["policy"]
        == {
            "parent_conversation_id": str(parent),
            "scope_id": str(scope),
            "runtime_session_id": str(session),
            "generation": 2,
            "manifest_digest": "b" * 64,
            "max_active_children": 3,
        }
        for tool in tools
    )
    manifest["tools"].clear()
    assert child_capability_manifest()["tools"] == list(CHILD_TOOL_NAMES)


def test_v3_environment_refuses_missing_capability_or_wrong_provenance():
    manifest = {
        "image_id": "sha256:" + "1" * 64,
        "runtime_provenance": {
            "source_commit": V3_SOURCE_COMMIT,
            "source_ref": V3_SOURCE_COMMIT,
            "source_archive_digest": V3_ARCHIVE_SHA256,
            "package_versions": {
                name: "1.58.2"
                for name in (
                    "openhands-agent-server",
                    "openhands-sdk",
                    "openhands-tools",
                    "openhands-workspace",
                )
            },
        },
        "execution_policy": {"llm_timeouts": FLOWWEAVE_LLM_TIMEOUT_POLICY.manifest_document()},
        "build": {
            "builder": "openhands.agent_server.docker.build",
            "target": "source-minimal",
            "platform": "linux/amd64",
            "user_base_image_reference": "test@sha256:abc",
            "user_base_image_digest": "sha256:abc",
            "runtime_image_digest": "sha256:" + "1" * 64,
        },
        "validation": {
            key: {"status": "PASSED"}
            for key in ("contract_check", "tool_workspace_probe", "security_scan")
        },
        "child_conversations": child_capability_manifest(),
    }
    validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V3")
    missing = deepcopy(manifest)
    missing.pop("child_conversations")
    with pytest.raises(DomainError):
        validate_runtime_manifest(missing, runtime_protocol_profile="OH_FW_V3")
    # Historical environments remain valid with their own provenance and policy.
    missing["runtime_provenance"]["source_commit"] = "a" * 40
    validate_runtime_manifest(missing, runtime_protocol_profile="OH_FW_V2")
    with pytest.raises(DomainError):
        validate_runtime_manifest(missing, runtime_protocol_profile="OH_FW_V3")
