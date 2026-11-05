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
from flowweave.runtime.base import FLOWWEAVE_LLM_TIMEOUT_POLICY, FLOWWEAVE_SUBAGENT_POLICY
from flowweave.runtime.child_contract import (
    CHILD_HTTP_OPERATIONS,
    CHILD_TOOL_NAMES,
    V3_ARCHIVE_SHA256,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
    require_child_capability_manifest,
)
from flowweave.runtime.child_governance_contract import (
    V4_ARCHIVE_SHA256,
    V4_CHILD_HTTP_OPERATIONS,
    V4_SOURCE_COMMIT,
    child_capability_build_evidence,
    governed_child_capability_manifest,
    require_governed_child_capability_manifest,
)
from flowweave.runtime.contract import normalize_runtime_contract, runtime_contract_document
from flowweave.runtime.protocol_profiles import (
    child_manifest_for_protocol_profile,
    runtime_contract_for_protocol_profile,
)
from flowweave.shared.domain.openhands import (
    OpenHandsServerIdentity,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import settings_context


def provenance():
    return {
        "source_commit": V4_SOURCE_COMMIT,
        "source_ref": V4_SOURCE_COMMIT,
        "source_archive_digest": V4_ARCHIVE_SHA256,
        "package_versions": {
            package: "1.58.2"
            for package in (
                "openhands-agent-server",
                "openhands-sdk",
                "openhands-tools",
                "openhands-workspace",
            )
        },
    }


def environment_manifest():
    digest = "sha256:" + "1" * 64
    return {
        "image_id": digest,
        "runtime_provenance": provenance(),
        "child_conversations": governed_child_capability_manifest(),
        "execution_policy": {"llm_timeouts": FLOWWEAVE_LLM_TIMEOUT_POLICY.manifest_document()},
        "build": {
            "builder": "openhands.agent_server.docker.build",
            "target": "source-minimal",
            "platform": "linux/amd64",
            "user_base_image_reference": "test@sha256:abc",
            "user_base_image_digest": "sha256:abc",
            "runtime_image_digest": digest,
        },
        "validation": {
            key: {"status": "PASSED"}
            for key in ("contract_check", "tool_workspace_probe", "security_scan")
        },
    }


def test_v4_adds_governance_without_reinterpreting_v3():
    v3_identity = OpenHandsServerIdentity("1.58.2", V3_SOURCE_COMMIT, V3_SOURCE_COMMIT)
    old_manifest = child_capability_manifest()
    assert old_manifest["digest"] == (
        "c50549595d98880f44c2f97edd7fed834c8ebef83ff67aa6f7e4fb9eaf3320a1"
    )
    assert child_manifest_for_protocol_profile("OH_FW_V3") == old_manifest
    manifest = child_manifest_for_protocol_profile("OH_FW_V4")
    assert manifest == governed_child_capability_manifest()
    assert manifest["version"] == "oh-fw-child-v2"
    assert manifest["digest"] != old_manifest["digest"]
    with pytest.raises(DomainError):
        require_child_capability_manifest(manifest)
    with pytest.raises(DomainError):
        require_governed_child_capability_manifest(old_manifest)
    old_contract = runtime_contract_for_protocol_profile("OH_FW_V3", ("terminal",), v3_identity)
    contract = runtime_contract_for_protocol_profile(
        "OH_FW_V4",
        ("terminal",),
        OpenHandsServerIdentity("1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT),
    )
    assert contract.schema_version == 7 and old_contract.schema_version == 6
    assert set(V4_CHILD_HTTP_OPERATIONS) <= set(contract.required_http_operations)
    assert set(V4_CHILD_HTTP_OPERATIONS) - set(CHILD_HTTP_OPERATIONS) == {
        ("POST", "/api/conversations/{conversation_id}/children/launcher/resolve"),
        (
            "POST",
            "/api/conversations/{conversation_id}/children/preparations/{creation_id}/governed",
        ),
    }
    assert not (set(V4_CHILD_HTTP_OPERATIONS) - set(CHILD_HTTP_OPERATIONS)) & set(
        old_contract.required_http_operations
    )
    assert (
        normalize_runtime_contract(
            runtime_contract_document(contract), required_tools=("terminal",)
        )
        == contract
    )
    for profile, identity in (
        ("OH_FW_V4", v3_identity),
        ("OH_FW_V3", OpenHandsServerIdentity("1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT)),
    ):
        with pytest.raises(DomainError):
            runtime_contract_for_protocol_profile(profile, ("terminal",), identity)


@pytest.mark.parametrize(
    "field", ["source_commit", "source_ref", "source_archive_digest", "package_versions"]
)
def test_v4_evidence_cannot_be_declared_for_wrong_or_missing_provenance(field):
    valid = provenance()
    assert child_capability_build_evidence(valid) == {
        "child_conversations": governed_child_capability_manifest()
    }
    manifest = environment_manifest()
    validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V4")
    manifest["runtime_provenance"].pop(field)
    assert child_capability_build_evidence(manifest["runtime_provenance"]) == {}
    with pytest.raises(DomainError):
        validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V4")


def test_v3_build_and_environment_evidence_retains_the_frozen_version():
    manifest = environment_manifest()
    manifest["runtime_provenance"].update(
        source_commit=V3_SOURCE_COMMIT,
        source_ref=V3_SOURCE_COMMIT,
        source_archive_digest=V3_ARCHIVE_SHA256,
    )
    manifest.update(child_capability_build_evidence(manifest["runtime_provenance"]))
    assert manifest["child_conversations"] == child_capability_manifest()
    validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V3")
    with pytest.raises(DomainError):
        validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V4")
    manifest.pop("child_conversations")
    validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V2")


@pytest.mark.parametrize("mutation", ["missing", "old", "policy", "digest"])
def test_v4_environment_refuses_missing_old_or_changed_capability(mutation):
    manifest = environment_manifest()
    if mutation == "missing":
        manifest.pop("child_conversations")
    elif mutation == "old":
        manifest["child_conversations"] = child_capability_manifest()
    else:
        changed = deepcopy(manifest["child_conversations"])
        if mutation == "policy":
            changed["preparation_governance"]["before_initialization"] = False
        else:
            changed["digest"] = "0" * 64
        manifest["child_conversations"] = changed
    with pytest.raises(DomainError):
        validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V4")


def test_v4_snapshot_and_spec_freeze_governance_without_installing_parent_tools(tmp_path: Path):
    definition = {"nodes": [{"instance_key": "node", "asset": {"id": "asset"}}]}
    frozen = _compile_runtime_manifest(
        definition,
        server_identity=OpenHandsServerIdentity("1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT),
        protocol_profile="OH_FW_V4",
    )
    assert frozen["child_conversations"] == governed_child_capability_manifest()
    config = FrozenSessionConfig(None, None, None, None, ())
    with settings_context(Settings(workspace_root=tmp_path)):
        with pytest.raises(DomainError, match="explicitly frozen"):
            build_agent_spec(
                config,
                provider=None,
                binding_id=str(uuid4()),
                working_directory="/workspace",
                host_root=tmp_path / "unidentified",
                runtime_root=Path("/runtime/capabilities"),
                protocol_profile="OH_FW_V4",
            )
        assert not (tmp_path / "unidentified").exists()
        spec = build_agent_spec(
            config,
            provider=None,
            binding_id=str(uuid4()),
            working_directory="/workspace",
            host_root=tmp_path / "identified",
            runtime_root=Path("/runtime/capabilities"),
            protocol_profile="OH_FW_V4",
            runtime_server_identity=OpenHandsServerIdentity(
                "1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT
            ),
        )
    assert spec.runtime_contract.source_commit == V4_SOURCE_COMMIT
    assert spec.subagent_policy == FLOWWEAVE_SUBAGENT_POLICY
    assert spec.llm_timeout_policy == FLOWWEAVE_LLM_TIMEOUT_POLICY
    assert set(CHILD_TOOL_NAMES).isdisjoint(tool.name for tool in spec.tools)
