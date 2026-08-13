from __future__ import annotations

import base64
import io
import json
import zipfile

from sqlalchemy import select

from flowweave.modules.catalog.application import plugin_validations
from flowweave.runtime.base import (
    RuntimePlugin,
    RuntimePluginValidationRequest,
    RuntimePluginValidationResult,
)
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityValidation, EnvironmentVersion, TerminalEnvironment


def _plugin_capability(client) -> dict:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            ".plugin/plugin.json",
            json.dumps({"name": "governed-review", "version": "1.0.0"}),
        )
        bundle.writestr(
            "commands/review.md",
            "---\nname: review\n---\nReview the change.\n",
        )
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "PLUGIN",
            "filename": "governed-review.zip",
            "content_base64": base64.b64encode(archive.getvalue()).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _environment_version(db_session_factory) -> str:
    with db_session_factory() as db:
        environment = TerminalEnvironment(
            name="Plugin target environment",
            base_image="flowweave-openhands-runtime:1",
        )
        db.add(environment)
        db.flush()
        version = EnvironmentVersion(
            environment_id=environment.id,
            version_no=1,
            state="READY",
            image_reference="flowweave/environment-plugin:v1",
            image_digest=f"sha256:{'b' * 64}",
            manifest_json={"schema_version": 1},
        )
        db.add(version)
        db.commit()
        return version.id


def _runtime_request(plan: plugin_validations.PluginProbePlan) -> RuntimePluginValidationRequest:
    normalized = plan.capability["normalized_config"]
    return RuntimePluginValidationRequest(
        plugin=RuntimePlugin(
            name=str(plan.capability["capability_key"]),
            source=(
                f"/runtime/capabilities/nodes/plugin-probe-{plan.validation_id}"
                "/plugins/governed-review"
            ),
            content_hash=str(normalized["content_hash"]),
        ),
        validation_id=plan.validation_id,
        runtime_resource_id="11111111-1111-4111-8111-111111111111",
        runtime_resource_name="fw-sbx-plugin-probe",
    )


def test_plugin_probe_projects_native_loader_metadata_only(client, db_session_factory, monkeypatch):
    capability = _plugin_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    cleaned: list[str] = []
    monkeypatch.setattr(
        plugin_validations,
        "allocate_probe_runtime",
        lambda _db, plan: _runtime_request(plan),
    )
    monkeypatch.setattr(
        plugin_validations,
        "cleanup_probe",
        lambda _db, validation_id: cleaned.append(validation_id),
    )
    monkeypatch.setattr(
        MockRuntime,
        "validate_plugin",
        lambda _self, _request: RuntimePluginValidationResult(
            plugin_name="governed-review",
            plugin_version="1.0.0",
            skill_count=0,
            command_count=1,
            agent_count=0,
            mcp_server_count=0,
            has_hooks=False,
        ),
    )

    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/plugin-probes",
        json={"environment_version_id": environment_version_id},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PASSED"
    assert body["environment_version_id"] == environment_version_id
    assert body["report"]["loader"] == "openhands.sdk.plugin.Plugin.load"
    assert body["report"]["plugin_name"] == "governed-review"
    assert body["report"]["contributions"] == {
        "skills": 0,
        "commands": 1,
        "agents": 0,
        "mcp_servers": 0,
        "hooks": False,
    }
    assert "Review the change" not in response.text
    assert cleaned == [body["id"]]


def test_plugin_probe_failure_is_durable_and_cleanup_still_runs(
    client, db_session_factory, monkeypatch
):
    capability = _plugin_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    cleaned: list[str] = []
    monkeypatch.setattr(
        plugin_validations,
        "allocate_probe_runtime",
        lambda _db, plan: _runtime_request(plan),
    )
    monkeypatch.setattr(
        plugin_validations,
        "cleanup_probe",
        lambda _db, validation_id: cleaned.append(validation_id),
    )

    def reject(_self, _request):
        raise DomainError(
            "PLUGIN_TARGET_VALIDATION_FAILED",
            "target loader rejected plugin",
            422,
        )

    monkeypatch.setattr(MockRuntime, "validate_plugin", reject)
    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/plugin-probes",
        json={"environment_version_id": environment_version_id},
    )

    assert response.status_code == 422
    with db_session_factory() as db:
        validation = db.scalar(
            select(CapabilityValidation).where(
                CapabilityValidation.validator == "openhands-plugin-target-v1"
            )
        )
        assert validation is not None
        assert validation.status == "FAILED"
        assert validation.report_json["error_code"] == "PLUGIN_TARGET_VALIDATION_FAILED"
        assert validation.completed_at is not None
        validation_id = validation.id
    assert cleaned == [validation_id]


def test_plugin_probe_rejects_non_plugin_capability(client, db_session_factory):
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("governed-skill/SKILL.md", "# Governed skill\n")
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "governed-skill.zip",
            "content_base64": base64.b64encode(source.getvalue()).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    skill = committed.json()["capabilities"][0]
    environment_version_id = _environment_version(db_session_factory)

    response = client.post(
        f"/api/v1/capabilities/{skill['capability_id']}/plugin-probes",
        json={"environment_version_id": environment_version_id},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLUGIN_CAPABILITY_REQUIRED"
