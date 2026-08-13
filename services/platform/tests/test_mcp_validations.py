from __future__ import annotations

import base64
import hashlib
import json

import pytest
from sqlalchemy import select

from flowweave.modules.catalog.application import (
    mcp_oauth_authorizations,
    mcp_oauth_secrets,
    mcp_validations,
)
from flowweave.runtime.base import (
    RuntimeMCP,
    RuntimeMCPOAuthStartRequest,
    RuntimeMCPOAuthStatus,
    RuntimeMCPProbeRequest,
    RuntimeMCPProbeResult,
)
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.credentials_crypto import encrypt_secret
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    CapabilityValidation,
    EnvironmentVersion,
    MCPOAuthAuthorization,
    MCPOAuthSecretAudit,
    MCPOAuthSecretReference,
    TerminalEnvironment,
)
from flowweave.shared.settings import settings_context


def _mcp_capability(client) -> dict:
    source = b'{"mcpServers":{"docs":{"url":"https://mcp.example.test/mcp"}}}'
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "docs.json",
            "content_base64": base64.b64encode(source).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _oauth_mcp_capability(client) -> dict:
    source = json.dumps(
        {
            "mcpServers": {
                "oauth-docs": {
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "strategy": "oauth2",
                        "authentication": {
                            "type": "oauth",
                            "client_name": "FlowWeave validation",
                        },
                    },
                }
            }
        }
    ).encode()
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "oauth-docs.json",
            "content_base64": base64.b64encode(source).decode(),
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
            name="MCP target environment",
            base_image="flowweave-openhands-runtime:1",
        )
        db.add(environment)
        db.flush()
        version = EnvironmentVersion(
            environment_id=environment.id,
            version_no=1,
            state="READY",
            image_reference="flowweave/environment-mcp:v1",
            image_digest=f"sha256:{'a' * 64}",
            manifest_json={"schema_version": 1},
        )
        db.add(version)
        db.commit()
        return version.id


def _probe_request(plan: mcp_validations.MCPProbePlan) -> RuntimeMCPProbeRequest:
    return RuntimeMCPProbeRequest(
        server=RuntimeMCP(
            name=plan.capability_key,
            config={"transport": "http", "url": "https://mcp.example.test/mcp"},
        ),
        base_url="http://fw-sbx-probe:8000",
        runtime_resource_name="fw-sbx-probe",
        timeout=plan.timeout,
        read_only_tool_call=plan.read_only_tool_call,
    )


def _oauth_probe_request(db, plan: mcp_validations.MCPProbePlan) -> RuntimeMCPProbeRequest:
    assert plan.oauth_secret_reference_id is not None
    assert plan.oauth_secret_version is not None
    state = mcp_oauth_secrets.load_state(
        db,
        secret_reference_id=plan.oauth_secret_reference_id,
        expected_state_version=plan.oauth_secret_version,
        capability_version_id=str(plan.capability["capability_version_id"]),
        environment_version_id=plan.environment_version_id,
    )
    return RuntimeMCPProbeRequest(
        server=RuntimeMCP(
            name=plan.capability_key,
            config={
                "transport": "http",
                "url": "https://mcp.example.test/mcp",
                "auth": {
                    "strategy": "oauth2",
                    "authentication": {"type": "oauth"},
                },
            },
        ),
        base_url="http://fw-sbx-probe:8000",
        runtime_resource_name="fw-sbx-probe",
        timeout=plan.timeout,
        oauth_secret_reference_id=plan.oauth_secret_reference_id,
        oauth_secret_version=plan.oauth_secret_version,
        oauth_state=state,
    )


def _oauth_authorization_request(
    db, plan: mcp_oauth_authorizations.AuthorizationStartPlan
) -> RuntimeMCPOAuthStartRequest:
    item = db.get(MCPOAuthAuthorization, plan.authorization_id)
    assert item is not None
    item.runtime_base_url = "http://fw-sbx-oauth:8000"
    item.runtime_resource_name = "fw-sbx-oauth"
    db.flush()
    return RuntimeMCPOAuthStartRequest(
        server=RuntimeMCP(
            name=plan.capability_key,
            config={
                "transport": "http",
                "url": "https://mcp.example.test/mcp",
                "auth": {
                    "strategy": "oauth2",
                    "authentication": {"type": "oauth"},
                },
            },
        ),
        base_url="http://fw-sbx-oauth:8000",
        runtime_resource_name="fw-sbx-oauth",
        timeout=plan.timeout,
    )


def test_mcp_probe_projects_catalog_and_only_hashes_read_only_result(
    client, db_session_factory, monkeypatch
):
    capability = _mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    cleaned: list[str] = []

    monkeypatch.setattr(
        mcp_validations,
        "allocate_probe_runtime",
        lambda _db, plan: _probe_request(plan),
    )
    monkeypatch.setattr(
        mcp_validations, "cleanup_probe", lambda _db, validation_id: cleaned.append(validation_id)
    )
    monkeypatch.setattr(
        MockRuntime,
        "probe_mcp",
        lambda _self, _request: RuntimeMCPProbeResult(
            ok=True,
            tools=("lookup", "status"),
            tool_call_is_error=False,
            tool_call_text="sensitive target data",
        ),
    )

    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={
            "environment_version_id": environment_version_id,
            "read_only_tool_call": {"name": "status", "arguments": {}},
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PASSED"
    assert body["environment_version_id"] == environment_version_id
    assert body["report"]["tool_catalog"] == [
        {
            "name": "lookup",
            "input_schema": None,
            "schema_status": "UNAVAILABLE_FROM_OPENHANDS_MCP_TEST",
        },
        {
            "name": "status",
            "input_schema": None,
            "schema_status": "UNAVAILABLE_FROM_OPENHANDS_MCP_TEST",
        },
    ]
    projected = body["report"]["read_only_tool_call"]
    assert projected == {
        "is_error": False,
        "result_bytes": len(b"sensitive target data"),
        "result_sha256": hashlib.sha256(b"sensitive target data").hexdigest(),
    }
    assert "sensitive target data" not in response.text
    assert cleaned == [body["id"]]


def test_mcp_probe_failure_is_durable_and_cleanup_still_runs(
    client, db_session_factory, monkeypatch
):
    capability = _mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    cleaned: list[str] = []

    monkeypatch.setattr(
        mcp_validations,
        "allocate_probe_runtime",
        lambda _db, plan: _probe_request(plan),
    )
    monkeypatch.setattr(
        mcp_validations, "cleanup_probe", lambda _db, validation_id: cleaned.append(validation_id)
    )

    def fail(_self, _request):
        raise DomainError("EXECUTOR_UNAVAILABLE", "target runtime unavailable", 503)

    monkeypatch.setattr(MockRuntime, "probe_mcp", fail)
    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={"environment_version_id": environment_version_id},
    )

    assert response.status_code == 503
    with db_session_factory() as db:
        validation = db.scalar(
            select(CapabilityValidation).where(
                CapabilityValidation.validator == "openhands-mcp-target-v1"
            )
        )
        assert validation is not None
        assert validation.status == "FAILED"
        assert validation.completed_at is not None
        assert validation.report_json["error_code"] == "EXECUTOR_UNAVAILABLE"
        validation_id = validation.id
    assert cleaned == [validation_id]


def test_mcp_probe_audit_prevents_target_environment_version_deletion(
    client, db_session_factory, monkeypatch
):
    capability = _mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    monkeypatch.setattr(
        mcp_validations,
        "allocate_probe_runtime",
        lambda _db, plan: _probe_request(plan),
    )
    monkeypatch.setattr(mcp_validations, "cleanup_probe", lambda _db, _validation_id: None)

    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={"environment_version_id": environment_version_id},
    )
    assert response.status_code == 201, response.text

    with db_session_factory() as db:
        version = db.get(EnvironmentVersion, environment_version_id)
        assert version is not None
        environment_id = version.environment_id

    blocked = client.delete(
        f"/api/v1/terminal-environments/{environment_id}/versions/{environment_version_id}"
    )
    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "ENVIRONMENT_VERSION_IN_USE"
    assert error["details"]["capability_validation_reference_count"] == 1


def test_mcp_oauth_secret_reference_refresh_revoke_and_leakage_boundaries(
    client, db_session_factory, monkeypatch
):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    created = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    )
    assert created.status_code == 201, created.text
    reference = created.json()
    assert reference["state"] == "ACTIVE"
    assert reference["state_version"] == 1
    assert reference["has_oauth_state"] is False

    oauth_state = {
        "tokens": {
            "access_token": "oauth-access-secret",
            "refresh_token": "oauth-refresh-secret",
            "token_type": "Bearer",
        },
        "client_info": {
            "client_id": "client-id",
            "client_secret": "oauth-client-secret",
        },
        "token_expires_at": 2_000_000_000.0,
    }
    captured_requests: list[RuntimeMCPProbeRequest] = []

    monkeypatch.setattr(mcp_validations, "allocate_probe_runtime", _oauth_probe_request)
    monkeypatch.setattr(mcp_validations, "cleanup_probe", lambda _db, _validation_id: None)

    def refreshed(_self, request):
        captured_requests.append(request)
        return RuntimeMCPProbeResult(ok=True, tools=("lookup",), oauth_state=oauth_state)

    monkeypatch.setattr(MockRuntime, "probe_mcp", refreshed)
    probed = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={
            "environment_version_id": environment_version_id,
            "oauth_secret_reference_id": reference["id"],
            "expected_oauth_secret_version": 1,
        },
    )
    assert probed.status_code == 201, probed.text
    assert captured_requests[0].oauth_state is None
    assert probed.json()["report"]["oauth_state_persisted"] is True
    assert probed.json()["report"]["oauth_secret_version"] == 2
    for secret in ("oauth-access-secret", "oauth-refresh-secret", "oauth-client-secret"):
        assert secret not in probed.text

    read = client.get(f"/api/v1/mcp-oauth-secret-references/{reference['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["has_oauth_state"] is True
    assert read.json()["state_version"] == 2
    assert [item["action"] for item in read.json()["audit"]] == ["CREATED", "REFRESHED"]
    for secret in ("oauth-access-secret", "oauth-refresh-secret", "oauth-client-secret"):
        assert secret not in read.text

    with db_session_factory() as db:
        stored = db.get(MCPOAuthSecretReference, reference["id"])
        assert stored is not None
        assert stored.encrypted_oauth_state is not None
        assert b"oauth-access-secret" not in stored.encrypted_oauth_state
        assert stored.oauth_state_digest is not None
        audits = list(
            db.scalars(
                select(MCPOAuthSecretAudit).where(
                    MCPOAuthSecretAudit.secret_reference_id == reference["id"]
                )
            )
        )
        assert all(audit.oauth_state_digest != "oauth-access-secret" for audit in audits)

    revoked = client.post(
        f"/api/v1/mcp-oauth-secret-references/{reference['id']}/revoke",
        json={"expected_state_version": 2},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "REVOKED"
    assert revoked.json()["state_version"] == 3
    assert revoked.json()["has_oauth_state"] is False

    rejected = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={
            "environment_version_id": environment_version_id,
            "oauth_secret_reference_id": reference["id"],
            "expected_oauth_secret_version": 3,
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "MCP_OAUTH_SECRET_REVOKED"


def test_mcp_oauth_refresh_cas_rejects_stale_result(db_session_factory, client, settings):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    created = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    ).json()
    state = {"tokens": {"access_token": "first-secret"}}
    with settings_context(settings), db_session_factory() as db:
        next_version = mcp_oauth_secrets.persist_refreshed_state(
            db,
            secret_reference_id=created["id"],
            expected_state_version=1,
            validation_id=None,
            state=state,
        )
        db.commit()
    assert next_version == 2

    with (
        settings_context(settings),
        db_session_factory() as db,
        pytest.raises(DomainError) as raised,
    ):
        mcp_oauth_secrets.persist_refreshed_state(
            db,
            secret_reference_id=created["id"],
            expected_state_version=1,
            validation_id=None,
            state={"tokens": {"access_token": "stale-secret"}},
        )
    assert raised.value.code == "MCP_OAUTH_SECRET_VERSION_CONFLICT"


def test_mcp_oauth_browser_authorization_persists_state_without_leaking_secrets(
    db_session_factory, client, monkeypatch
):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    reference = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    ).json()
    authorization_url = "https://identity.example.test/authorize?state=browser-state-secret"
    callback_url = (
        "http://localhost:54321/callback?code=authorization-code-secret&state=browser-state-secret"
    )
    oauth_state = {
        "tokens": {
            "access_token": "browser-access-secret",
            "refresh_token": "browser-refresh-secret",
        },
        "client_info": {
            "client_id": "browser-client",
            "client_secret": "browser-client-secret",
        },
    }
    captured_callbacks: list[str] = []

    monkeypatch.setattr(
        mcp_oauth_authorizations,
        "allocate_authorization_runtime",
        _oauth_authorization_request,
    )
    monkeypatch.setattr(
        mcp_oauth_authorizations,
        "cleanup_terminal",
        lambda _db, _authorization_id: None,
    )
    monkeypatch.setattr(
        MockRuntime,
        "start_mcp_oauth",
        lambda _self, _request: RuntimeMCPOAuthStatus(
            ok=True,
            status="authorizing",
            job_id="runtime-oauth-job",
            authorization_url=authorization_url,
        ),
    )
    monkeypatch.setattr(
        MockRuntime,
        "read_mcp_oauth",
        lambda _self, request: RuntimeMCPOAuthStatus(
            ok=True,
            status="authorizing",
            job_id=request.job_id,
            authorization_url=authorization_url,
            callback_ready=True,
        ),
    )

    def finish(_self, request):
        captured_callbacks.append(request.callback_url)
        return RuntimeMCPOAuthStatus(
            ok=True,
            status="succeeded",
            job_id=request.job_id,
            callback_ready=True,
            tools=("lookup", "status"),
            oauth_state=oauth_state,
        )

    monkeypatch.setattr(MockRuntime, "submit_mcp_oauth_callback", finish)

    started = client.post(
        f"/api/v1/mcp-oauth-secret-references/{reference['id']}/authorizations",
        json={"expected_state_version": 1, "timeout": 12},
    )
    assert started.status_code == 202, started.text
    authorization = started.json()
    assert authorization["state"] == "AUTHORIZING"
    assert authorization["state_version"] == 2
    assert authorization["authorization_url"] == authorization_url
    assert "access_token" not in started.text

    polled = client.get(f"/api/v1/mcp-oauth-authorizations/{authorization['id']}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["callback_ready"] is True
    assert polled.json()["state_version"] == 3

    completed = client.post(
        f"/api/v1/mcp-oauth-authorizations/{authorization['id']}/callback",
        json={
            "expected_authorization_version": 3,
            "callback_url": callback_url,
        },
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["state"] == "SUCCEEDED"
    assert body["state_version"] == 4
    assert body["persisted_secret_version"] == 2
    assert body["authorization_url"] is None
    assert body["tool_catalog"] == ["lookup", "status"]
    assert captured_callbacks == [callback_url]
    for secret in (
        "authorization-code-secret",
        "browser-access-secret",
        "browser-refresh-secret",
        "browser-client-secret",
    ):
        assert secret not in completed.text

    read_reference = client.get(f"/api/v1/mcp-oauth-secret-references/{reference['id']}")
    assert read_reference.status_code == 200, read_reference.text
    assert read_reference.json()["has_oauth_state"] is True
    assert read_reference.json()["state_version"] == 2
    assert [item["action"] for item in read_reference.json()["audit"]] == [
        "CREATED",
        "AUTHORIZED",
    ]

    with db_session_factory() as db:
        stored = db.get(MCPOAuthSecretReference, reference["id"])
        auth_job = db.get(MCPOAuthAuthorization, authorization["id"])
        assert stored is not None and stored.encrypted_oauth_state is not None
        assert auth_job is not None
        assert auth_job.encrypted_authorization_url is None
        assert callback_url not in str(auth_job.__dict__)
        for secret in (
            b"browser-access-secret",
            b"browser-refresh-secret",
            b"browser-client-secret",
        ):
            assert secret not in stored.encrypted_oauth_state


def test_mcp_oauth_revoke_fences_inflight_authorization(db_session_factory, client, monkeypatch):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    reference = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    ).json()
    monkeypatch.setattr(
        mcp_oauth_authorizations,
        "allocate_authorization_runtime",
        _oauth_authorization_request,
    )
    monkeypatch.setattr(
        MockRuntime,
        "start_mcp_oauth",
        lambda _self, _request: RuntimeMCPOAuthStatus(
            ok=True,
            status="authorizing",
            job_id="runtime-oauth-job",
            authorization_url="https://identity.example.test/authorize",
        ),
    )
    started = client.post(
        f"/api/v1/mcp-oauth-secret-references/{reference['id']}/authorizations",
        json={"expected_state_version": 1},
    )
    assert started.status_code == 202, started.text
    cleanup_observed_states: list[str] = []

    def observe_committed_revoke(_db, _sandbox_id):
        with db_session_factory() as verification_db:
            stored = verification_db.get(MCPOAuthSecretReference, reference["id"])
            assert stored is not None
            cleanup_observed_states.append(stored.state)

    monkeypatch.setattr(
        mcp_oauth_secrets,
        "request_delete_durable",
        observe_committed_revoke,
    )
    monkeypatch.setattr(mcp_oauth_secrets, "cleanup_mcp_probe", lambda _id: None)

    revoked = client.post(
        f"/api/v1/mcp-oauth-secret-references/{reference['id']}/revoke",
        json={"expected_state_version": 1},
    )
    assert revoked.status_code == 200, revoked.text
    assert cleanup_observed_states == ["REVOKED"]
    auth_read = client.get(f"/api/v1/mcp-oauth-authorizations/{started.json()['id']}")
    assert auth_read.status_code == 200, auth_read.text
    assert auth_read.json()["state"] == "FAILED"
    assert auth_read.json()["error_code"] == "MCP_OAUTH_SECRET_REVOKED"
    assert auth_read.json()["authorization_url"] is None

    callback = client.post(
        f"/api/v1/mcp-oauth-authorizations/{started.json()['id']}/callback",
        json={
            "expected_authorization_version": auth_read.json()["state_version"],
            "callback_url": "http://localhost:54321/callback?code=stale-code",
        },
    )
    assert callback.status_code == 409, callback.text
    assert callback.json()["error"]["code"] == "MCP_OAUTH_AUTHORIZATION_TERMINAL"


def test_mcp_oauth_corrupt_stored_state_fails_closed(db_session_factory, client, settings):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    created = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    ).json()

    with settings_context(settings), db_session_factory() as db:
        item = db.get(MCPOAuthSecretReference, created["id"])
        assert item is not None
        item.encrypted_oauth_state = encrypt_secret('{"unexpected":true}')
        db.commit()

    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={
            "environment_version_id": environment_version_id,
            "oauth_secret_reference_id": created["id"],
            "expected_oauth_secret_version": 1,
        },
    )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "MCP_OAUTH_SECRET_CORRUPT"


def test_mcp_oauth_state_digest_mismatch_fails_closed(db_session_factory, client, settings):
    capability = _oauth_mcp_capability(client)
    environment_version_id = _environment_version(db_session_factory)
    created = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-oauth-secret-references",
        json={"environment_version_id": environment_version_id},
    ).json()

    with settings_context(settings), db_session_factory() as db:
        item = db.get(MCPOAuthSecretReference, created["id"])
        assert item is not None
        state = '{"tokens":{"access_token":"valid-shape"}}'
        item.encrypted_oauth_state = encrypt_secret(state)
        item.oauth_state_digest = "0" * 64
        db.commit()

    response = client.post(
        f"/api/v1/capabilities/{capability['capability_id']}/mcp-probes",
        json={
            "environment_version_id": environment_version_id,
            "oauth_secret_reference_id": created["id"],
            "expected_oauth_secret_version": 1,
        },
    )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "MCP_OAUTH_SECRET_CORRUPT"


def test_mcp_import_rejects_embedded_oauth_state(client):
    source = json.dumps(
        {
            "mcpServers": {
                "unsafe": {
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "strategy": "oauth2",
                        "state": {"tokens": {"access_token": "must-not-persist"}},
                    },
                }
            }
        }
    ).encode()
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "unsafe.json",
            "content_base64": base64.b64encode(source).decode(),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMPORT_REJECTED"
    assert "must-not-persist" not in response.text
