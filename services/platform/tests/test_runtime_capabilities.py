from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from flowweave.modules.environments.application import service
from flowweave.modules.environments.infrastructure import docker
from flowweave.runtime.manifest import runtime_manifest_hash, runtime_node
from flowweave.shared.domain.openhands import OPENHANDS_SOURCE_COMMIT
from flowweave.shared.domain.runtime_capabilities import (
    normalize_runtime_capabilities,
    openhands_install_capabilities,
    runtime_capability_profile,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import EnvironmentPublishWrite


@pytest.fixture(autouse=True)
def database():
    """Pure Environment build-spec checks do not require Testcontainers."""

    yield


@pytest.mark.parametrize(
    ("selected", "canonical", "profile"),
    (
        ((), (), "minimal"),
        (("browser",), ("browser",), "browser"),
        (("docker", "vscode"), ("vscode", "docker"), "vscode+docker"),
        (("docker", "browser", "vscode"), ("vscode", "browser", "docker"), "vscode+browser+docker"),
    ),
)
def test_governed_runtime_capability_sets_are_canonical(
    selected: tuple[str, ...], canonical: tuple[str, ...], profile: str
) -> None:
    assert normalize_runtime_capabilities(selected) == canonical
    assert openhands_install_capabilities(selected) == ",".join(canonical)
    assert runtime_capability_profile(selected) == profile


@pytest.mark.parametrize("selected", (("browser", "browser"), ("shell",)))
def test_governed_runtime_capability_sets_reject_duplicates_and_unknowns(
    selected: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        normalize_runtime_capabilities(selected)
    with pytest.raises(ValueError):
        EnvironmentPublishWrite(runtime_capabilities=list(selected))


def test_publish_schema_rejects_upstream_docker_build_args() -> None:
    with pytest.raises(ValueError):
        EnvironmentPublishWrite.model_validate(
            {"runtime_capabilities": ["browser"], "install_capabilities": "docker"}
        )


def test_formal_build_receives_only_the_canonical_governed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: dict[str, object] = {}
    monkeypatch.setattr(
        docker,
        "get_settings",
        lambda: SimpleNamespace(
            docker_binary="docker",
            openhands_runtime_builder_image="flowweave-openhands-runtime:1",
            terminal_environment_publish_timeout_seconds=60,
        ),
    )

    def fake_run(command: list[str], **_kwargs: object) -> str:
        captured_options.update(json.loads(command[-1]))
        return (
            'FLOWWEAVE_OPENHANDS_BUILD={"tags":'
            '["flowweave/environment-environment-1-runtime:v1-version1"],"telemetry":{}}'
        )

    monkeypatch.setattr(docker, "_run", fake_run)
    built = docker._build_openhands_runtime(
        base_image="flowweave/environment-environment-1-base:v1-version1",
        environment_id="environment-1",
        version_id="version-1",
        version_no=1,
        platform="linux/amd64",
        runtime_capabilities=("docker", "browser"),
    )

    assert captured_options["target"] == "source"
    assert captured_options["install_capabilities"] == "browser,docker"
    assert "build_args" not in captured_options
    assert built.install_capabilities == "browser,docker"
    assert built.capability_profile == "browser+docker"


def _manifest(*, target: str, install_capabilities: str, profile: str) -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    return {
        "image_id": digest,
        "runtime_provenance": {
            "package_versions": {
                "openhands-agent-server": "1.47.0",
                "openhands-sdk": "1.47.0",
                "openhands-tools": "1.47.0",
                "openhands-workspace": "1.47.0",
            },
            "source_commit": OPENHANDS_SOURCE_COMMIT,
            "source_ref": OPENHANDS_SOURCE_COMMIT,
            "source_archive_digest": (
                "70128f691ba58f0a1a1f6987c24738bb144209c61ba1b44a349a5504a98ea6b5"
            ),
            "overlays": {},
        },
        "build": {
            "builder": "openhands.agent_server.docker.build",
            "target": target,
            "platform": "linux/amd64",
            "user_base_image_reference": "python@sha256:" + "1" * 64,
            "user_base_image_digest": "sha256:" + "1" * 64,
            "runtime_image_digest": digest,
            "install_capabilities": install_capabilities,
            "capability_profile": profile,
        },
        "validation": {
            "contract_check": {"status": "PASSED"},
            "tool_workspace_probe": {"status": "PASSED"},
            "security_scan": {"status": "NOT_RUN"},
        },
    }


def test_manifest_rejects_capability_target_or_profile_drift() -> None:
    service.validate_runtime_manifest(
        _manifest(target="source", install_capabilities="browser,docker", profile="browser+docker")
    )
    for invalid in (
        _manifest(target="source-minimal", install_capabilities="browser", profile="browser"),
        _manifest(target="source", install_capabilities="docker,browser", profile="browser+docker"),
        _manifest(target="source", install_capabilities="browser", profile="docker"),
    ):
        with pytest.raises(DomainError, match="capability build spec"):
            service.validate_runtime_manifest(invalid)

    with pytest.raises(DomainError, match="differs from its frozen version"):
        service.validate_runtime_manifest(
            _manifest(
                target="source",
                install_capabilities="browser,docker",
                profile="browser+docker",
            ),
            expected_runtime_capabilities=(),
        )


def test_snapshot_node_identity_ignores_openhands_version_metadata() -> None:
    definition = {
        "nodes": [
            {
                "instance_key": "node-1",
                "node_asset_id": "asset-1",
                "asset": {"id": "asset-1"},
            }
        ]
    }
    manifest = {
        "schema_version": 3,
        "openhands_version": "1.44.0",
        "nodes": {"node-1": {"node_asset_id": "asset-1"}},
    }
    kwargs = {
        "definition": definition,
        "manifest": manifest,
        "expected_hash": runtime_manifest_hash(manifest),
        "snapshot_id": "snapshot-1",
        "instance_key": "node-1",
    }

    node = runtime_node(**kwargs)
    assert node["runtime_snapshot_id"] == "snapshot-1"

    manifest["nodes"]["node-1"]["tool_policy"] = {}
    kwargs["expected_hash"] = runtime_manifest_hash(manifest)
    with pytest.raises(DomainError, match="retired Agent Tool Policy"):
        runtime_node(**kwargs)
