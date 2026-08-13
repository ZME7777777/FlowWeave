from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from flowweave.modules.catalog.application import mcp_oauth_secrets
from flowweave.modules.catalog.application.capability_repository import resolve_version
from flowweave.modules.environments.public import lock_referenceable_version
from flowweave.modules.sandboxes.public import create_runtime_sandbox, request_delete_durable
from flowweave.runtime.base import (
    RuntimeMCPProbeRequest,
    RuntimeMCPProbeResult,
    RuntimeMCPToolCall,
)
from flowweave.runtime.workspace import cleanup_mcp_probe, materialize_mcp_probe
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityValidation
from flowweave.shared.schemas import MCPProbeWrite

_VALIDATOR = "openhands-mcp-target-v1"


@dataclass(frozen=True, slots=True)
class MCPProbePlan:
    validation_id: str
    capability_key: str
    capability: dict[str, Any]
    environment_id: str
    environment_version_id: str
    environment_version_no: int
    image_digest: str
    timeout: float
    read_only_tool_call: RuntimeMCPToolCall | None
    oauth_secret_reference_id: str | None
    oauth_secret_version: int | None


def begin_probe(db: Session, capability_version_id: str, payload: MCPProbeWrite) -> MCPProbePlan:
    capability = resolve_version(db, capability_version_id)
    if capability.package.capability_type != "MCP":
        raise DomainError(
            "MCP_CAPABILITY_REQUIRED",
            "Target-environment MCP validation requires an MCP Capability Version",
            422,
        )
    environment = lock_referenceable_version(db, payload.environment_version_id)
    if environment is None:
        raise DomainError(
            "ENVIRONMENT_VERSION_INVALID",
            "MCP validation requires an active READY Environment Version",
            422,
            {"environment_version_id": payload.environment_version_id},
        )
    runtime_config = capability.runtime_config()
    if payload.oauth_secret_reference_id is not None:
        mcp_oauth_secrets.require_oauth_capability(runtime_config)
    elif (
        isinstance(runtime_config.get("auth"), dict)
        and runtime_config["auth"].get("strategy") == "oauth2"
    ):
        raise DomainError(
            "MCP_OAUTH_SECRET_REFERENCE_REQUIRED",
            "OAuth MCP validation requires a governed Secret Reference",
            422,
        )
    validation_id = uid()
    call = (
        RuntimeMCPToolCall(
            name=payload.read_only_tool_call.name,
            arguments=dict(payload.read_only_tool_call.arguments),
        )
        if payload.read_only_tool_call is not None
        else None
    )
    db.add(
        CapabilityValidation(
            id=validation_id,
            capability_version_id=capability.version.id,
            environment_version_id=environment.id,
            validator=_VALIDATOR,
            status="RUNNING",
            report_json={
                "schema_version": 1,
                "phase": "TARGET_RUNTIME_PROBE",
                "tool_schema_status": "UNAVAILABLE_FROM_OPENHANDS_MCP_TEST",
                "read_only_tool_call": call.name if call is not None else None,
                "oauth_secret_reference_id": payload.oauth_secret_reference_id,
                "oauth_secret_version": payload.expected_oauth_secret_version,
            },
        )
    )
    db.flush()
    return MCPProbePlan(
        validation_id=validation_id,
        capability_key=capability.package.capability_key,
        capability=runtime_config,
        environment_id=environment.environment_id,
        environment_version_id=environment.id,
        environment_version_no=environment.version_no,
        image_digest=environment.image_digest,
        timeout=payload.timeout,
        read_only_tool_call=call,
        oauth_secret_reference_id=payload.oauth_secret_reference_id,
        oauth_secret_version=payload.expected_oauth_secret_version,
    )


def allocate_probe_runtime(db: Session, plan: MCPProbePlan) -> RuntimeMCPProbeRequest:
    oauth_state = None
    if plan.oauth_secret_reference_id is not None and plan.oauth_secret_version is not None:
        oauth_state = mcp_oauth_secrets.load_state(
            db,
            secret_reference_id=plan.oauth_secret_reference_id,
            expected_state_version=plan.oauth_secret_version,
            capability_version_id=str(plan.capability["capability_version_id"]),
            environment_version_id=plan.environment_version_id,
        )
    server, workspace_relative = materialize_mcp_probe(
        {
            "capability_key": plan.capability_key,
            "normalized_config": plan.capability,
        },
        plan.validation_id,
    )
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
    return RuntimeMCPProbeRequest(
        server=server,
        base_url=allocation.base_url,
        runtime_resource_name=allocation.resource_name,
        timeout=plan.timeout,
        read_only_tool_call=plan.read_only_tool_call,
        oauth_secret_reference_id=plan.oauth_secret_reference_id,
        oauth_secret_version=plan.oauth_secret_version,
        oauth_state=oauth_state,
    )


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


def complete_probe(
    db: Session, validation_id: str, result: RuntimeMCPProbeResult
) -> dict[str, Any]:
    item = db.get(CapabilityValidation, validation_id)
    if item is None or item.validator != _VALIDATOR:
        raise DomainError("CAPABILITY_VALIDATION_NOT_FOUND", "MCP validation was not found", 404)
    if item.status != "RUNNING":
        return _validation_dict(item)
    tool_call: dict[str, Any] | None = None
    if result.tool_call_is_error is not None:
        text = result.tool_call_text or ""
        tool_call = {
            "is_error": result.tool_call_is_error,
            "result_bytes": len(text.encode()),
            "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    oauth_reference_id = item.report_json.get("oauth_secret_reference_id")
    oauth_secret_version = item.report_json.get("oauth_secret_version")
    persisted_oauth_version: int | None = None
    if result.oauth_state is not None:
        if not isinstance(oauth_reference_id, str) or not isinstance(oauth_secret_version, int):
            raise DomainError(
                "MCP_OAUTH_SECRET_REFERENCE_REQUIRED",
                "OpenHands returned OAuth state without a governed Secret Reference",
                502,
            )
        persisted_oauth_version = mcp_oauth_secrets.persist_refreshed_state(
            db,
            secret_reference_id=oauth_reference_id,
            expected_state_version=oauth_secret_version,
            validation_id=item.id,
            state=result.oauth_state,
        )
    item.status = "PASSED" if result.ok and result.tool_call_is_error is not True else "FAILED"
    item.report_json = {
        "schema_version": 1,
        "phase": "TARGET_RUNTIME_PROBE",
        "connection_ok": result.ok,
        "error_kind": result.error_kind,
        "tool_catalog": [
            {
                "name": name,
                "input_schema": None,
                "schema_status": "UNAVAILABLE_FROM_OPENHANDS_MCP_TEST",
            }
            for name in result.tools
        ],
        "read_only_tool_call": tool_call,
        "oauth_state_persisted": persisted_oauth_version is not None,
        "oauth_secret_reference_id": oauth_reference_id,
        "oauth_secret_version": persisted_oauth_version or oauth_secret_version,
    }
    item.completed_at = datetime.now(UTC)
    db.flush()
    return _validation_dict(item)


def fail_probe(db: Session, validation_id: str, error_code: str) -> dict[str, Any]:
    item = db.get(CapabilityValidation, validation_id)
    if item is None or item.validator != _VALIDATOR:
        raise DomainError("CAPABILITY_VALIDATION_NOT_FOUND", "MCP validation was not found", 404)
    if item.status == "RUNNING":
        previous_report = item.report_json or {}
        item.status = "FAILED"
        item.report_json = {
            "schema_version": 1,
            "phase": "TARGET_RUNTIME_PROBE",
            "connection_ok": False,
            "error_code": error_code,
            "tool_catalog": [],
            "tool_schema_status": "UNAVAILABLE_FROM_OPENHANDS_MCP_TEST",
            "oauth_state_persisted": False,
            "oauth_secret_reference_id": previous_report.get("oauth_secret_reference_id"),
            "oauth_secret_version": previous_report.get("oauth_secret_version"),
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
    cleanup_mcp_probe(validation_id)


def validation_owner_is_active(db: Session, validation_id: str) -> bool:
    item = db.get(CapabilityValidation, validation_id)
    return item is not None and item.validator == _VALIDATOR and item.status == "RUNNING"
