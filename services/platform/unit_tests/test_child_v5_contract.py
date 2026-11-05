"""The deployed baseline adds a profile without changing frozen V3/V4 identities."""

from copy import deepcopy

import pytest
from test_child_governance_contract import environment_manifest

from flowweave.modules.environments.application.service import validate_runtime_manifest
from flowweave.runtime.child_governance_contract import (
    V4_SOURCE_COMMIT,
    V5_ARCHIVE_SHA256,
    V5_SOURCE_COMMIT,
    child_capability_build_evidence,
    governed_child_capability_manifest,
)
from flowweave.runtime.protocol_profiles import runtime_contract_for_protocol_profile
from flowweave.shared.domain.openhands import OpenHandsServerIdentity
from flowweave.shared.errors import DomainError


def test_v5_accepts_deployed_provenance_and_rejects_cross_profile_reinterpretation():
    manifest = environment_manifest()
    old = deepcopy(manifest)
    manifest["runtime_provenance"].update(
        source_commit=V5_SOURCE_COMMIT,
        source_ref=V5_SOURCE_COMMIT,
        source_archive_digest=V5_ARCHIVE_SHA256,
    )
    manifest["child_conversations"] = governed_child_capability_manifest("OH_FW_V5")
    validate_runtime_manifest(manifest, runtime_protocol_profile="OH_FW_V5")
    assert child_capability_build_evidence(manifest["runtime_provenance"]) == {
        "child_conversations": manifest["child_conversations"]
    }
    validate_runtime_manifest(old, runtime_protocol_profile="OH_FW_V4")
    for document, profile in ((old, "OH_FW_V5"), (manifest, "OH_FW_V4")):
        with pytest.raises(DomainError):
            validate_runtime_manifest(document, runtime_protocol_profile=profile)
    for profile, source, schema in (
        ("OH_FW_V4", V4_SOURCE_COMMIT, 7),
        ("OH_FW_V5", V5_SOURCE_COMMIT, 8),
    ):
        identity = OpenHandsServerIdentity("1.58.2", source, source)
        assert (
            runtime_contract_for_protocol_profile(profile, ("terminal",), identity).schema_version
            == schema
        )
        opposite = "OH_FW_V5" if profile == "OH_FW_V4" else "OH_FW_V4"
        with pytest.raises(DomainError):
            runtime_contract_for_protocol_profile(opposite, (), identity)
