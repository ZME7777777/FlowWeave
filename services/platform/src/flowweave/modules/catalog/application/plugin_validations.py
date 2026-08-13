from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.modules.environments.public import lock_referenceable_version
from flowweave.modules.sandboxes.public import create_runtime_sandbox, request_delete_durable
from flowweave.runtime.base import (
    RuntimePluginValidationRequest,
    RuntimePluginValidationResult,
)
from flowweave.runtime.workspace import cleanup_plugin_probe, materialize_plugin_probe
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityValidation
from flowweave.shared.schemas import PluginProbeWrite

_VALIDATOR = "openhands-plugin-target-v1"


@dataclass(frozen=True, slots=True)
class PluginProbePlan:
    validation_id: str
    capability: dict[str, Any]
    environment_id: str
    environment_version_id: str
    environment_version_no: int
    image_digest: str


def _validation_dict(item: CapabilityValidation) -> dict[str, Any]:
    return {
        "id": item.id,
        "capability_version_id": item.capability_version_id,
        "environment_version_id": item.environment_version_id,
        "validator": item.validator,
        "status": item.status,
        "report": item.report_json or {},
        "created_at": item.created_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def begin_probe(
    db: Session, capability_version_id: str, payload: PluginProbeWrite
) -> PluginProbePlan:
    capability = resolve_version(db, capability_version_id)
    if capability.package.capability_type != "PLUGIN":
        raise DomainError(
            "PLUGIN_CAPABILITY_REQUIRED",
            "Target-environment Plugin validation requires a Plugin Capability Version",
            422,
        )
    environment = lock_referenceable_version(db, payload.environment_version_id)
    if environment is None:
        raise DomainError(
            "ENVIRONMENT_VERSION_INVALID",
            "Plugin validation requires an active READY Environment Version",
            422,
            {"environment_version_id": payload.environment_version_id},
        )
    validation_id = uid()
    runtime_config = capability.runtime_config()
    db.add(
        CapabilityValidation(
            id=validation_id,
            capability_version_id=capability.version.id,
            environment_version_id=environment.id,
            validator=_VALIDATOR,
            status="RUNNING",
            report_json={
                "schema_version": 1,
                "phase": "TARGET_RUNTIME_NATIVE_LOADER",
                "content_hash": runtime_config.get("content_hash"),
            },
        )
    )
    db.flush()
    return PluginProbePlan(
        validation_id=validation_id,
        capability={
            "capability_key": capability.package.capability_key,
            "normalized_config": runtime_config,
        },
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        image_digest=environment.image_digest,
    )


def allocate_probe_runtime(db: Session, plan: PluginProbePlan) -> RuntimePluginValidationRequest:
    plugin, workspace_relative = materialize_plugin_probe(plan.capability, plan.validation_id)
    allocation = create_runtime_sandbox(
        db,
        owner_type="CAPABILITY_VALIDATION",
        owner_id=plan.validation_id,
        image=plan.image_digest,
        environment_id=plan.environment_id,
        environment_version_id=plan.environment_version_id,
        environment_version_no=plan.environment_version_no,
        workspace_relative=workspace_relative,
    )
    return RuntimePluginValidationRequest(
        plugin=plugin,
        validation_id=plan.validation_id,
        runtime_resource_id=allocation.id,
        runtime_resource_name=allocation.resource_name,
    )


def complete_probe(
    db: Session, validation_id: str, result: RuntimePluginValidationResult
) -> dict[str, Any]:
    item = db.get(CapabilityValidation, validation_id)
    if item is None or item.validator != _VALIDATOR:
        raise DomainError("CAPABILITY_VALIDATION_NOT_FOUND", "Plugin validation was not found", 404)
    if item.status != "RUNNING":
        return _validation_dict(item)
    content_hash = (item.report_json or {}).get("content_hash")
    item.status = "PASSED"
    item.report_json = {
        "schema_version": 1,
        "phase": "TARGET_RUNTIME_NATIVE_LOADER",
        "loader": "openhands.sdk.plugin.Plugin.load",
        "content_hash": content_hash,
        "plugin_name": result.plugin_name,
        "plugin_version": result.plugin_version,
        "contributions": {
            "skills": result.skill_count,
            "commands": result.command_count,
            "agents": result.agent_count,
            "mcp_servers": result.mcp_server_count,
            "hooks": result.has_hooks,
        },
    }
    item.completed_at = datetime.now(UTC)
    db.flush()
    return _validation_dict(item)


def fail_probe(db: Session, validation_id: str, error_code: str) -> dict[str, Any]:
    item = db.get(CapabilityValidation, validation_id)
    if item is None or item.validator != _VALIDATOR:
        raise DomainError("CAPABILITY_VALIDATION_NOT_FOUND", "Plugin validation was not found", 404)
    if item.status == "RUNNING":
        content_hash = (item.report_json or {}).get("content_hash")
        item.status = "FAILED"
        item.report_json = {
            "schema_version": 1,
            "phase": "TARGET_RUNTIME_NATIVE_LOADER",
            "content_hash": content_hash,
            "error_code": error_code,
        }
        item.completed_at = datetime.now(UTC)
        db.flush()
    return _validation_dict(item)


def cleanup_probe(db: Session, validation_id: str) -> None:
    from flowweave.modules.sandboxes.public import latest_runtime_sandbox_snapshot

    snapshot = latest_runtime_sandbox_snapshot(
        db, owner_type="CAPABILITY_VALIDATION", owner_id=validation_id
    )
    if snapshot is not None:
        request_delete_durable(db, str(snapshot["id"]))
    cleanup_plugin_probe(validation_id)
