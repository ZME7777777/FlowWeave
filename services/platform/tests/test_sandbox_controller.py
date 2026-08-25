from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flowweave.bootstrap import runtime_provider as controller_module
from flowweave.bootstrap.runtime_provider import create_app
from flowweave.modules.environments.infrastructure import docker as environments_docker
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
    backend_name,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure import docker_controller as docker_controller_module

_API_KEY = "controller-api-test-key-with-at-least-32-bytes"
_WORKER_KEY = "controller-worker-test-key-with-at-least-32-bytes"
_SCOPE = "controller-test"
_RESOURCE_ID = "12345678-1234-4234-9234-123456789abc"
_OWNER_ID = "87654321-4321-4321-8321-cba987654321"
_ENVIRONMENT_ID = "11111111-1111-4111-8111-111111111111"
_ENVIRONMENT_VERSION_ID = "22222222-2222-4222-8222-222222222222"


def _settings(settings):
    return settings.model_copy(
        update={
            "terminal_environment_backend": "docker",
            "sandbox_manager_scope": _SCOPE,
            "docker_controller_api_key": _API_KEY,
            "docker_controller_worker_api_key": _WORKER_KEY,
        }
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_WORKER_KEY}"}


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"}


def _ensure_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "manager_scope": _SCOPE,
        "id": _RESOURCE_ID,
        "kind": "AGENT_RUNTIME",
        "owner_type": "CAPABILITY_VALIDATION",
        "owner_id": _OWNER_ID,
        "backend_resource_name": "fw-sbx-12345678123442349234123456789abc",
        "image_reference": "sha256:" + "a" * 64,
        "spec": {
            "workspace_relative": "nodes/node-1",
            "port": 8000,
            "environment_id": _ENVIRONMENT_ID,
            "environment_version_id": _ENVIRONMENT_VERSION_ID,
            "environment_version_no": 1,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload.update(updates)
    return payload


def _agent_workspace_runtime_payload() -> dict[str, object]:
    return {
        "manager_scope": _SCOPE,
        "id": _RESOURCE_ID,
        "kind": "AGENT_RUNTIME",
        "owner_type": "AGENT_WORKSPACE",
        "owner_id": _OWNER_ID,
        "backend_resource_name": backend_name(
            _RESOURCE_ID, owner_type="AGENT_WORKSPACE", owner_id=_OWNER_ID
        ),
        "image_reference": "sha256:" + "a" * 64,
        "runtime_secret_key": "x" * 32,
        "spec": {
            "agent_workspace_id": _OWNER_ID,
            "runtime_allocation_id": _ENVIRONMENT_ID,
            "runtime_allocation_relative": ".agent-workspaces/platform-default",
            "runtime_secret_reference_id": _ENVIRONMENT_VERSION_ID,
            "port": 8000,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }


def test_controller_rejects_unauthenticated_control_request(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post("/v1/sandboxes/ensure", json=_ensure_payload())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CONTROLLER_UNAUTHORIZED"
    assert touched == []


def test_controller_accepts_persistent_agent_workspace_runtime(settings, monkeypatch):
    received: list[tuple[str, str, str | None]] = []

    def ensure_running(_self, resource, *, runtime_secret_key=None):
        received.append(
            (resource.owner_type, resource.agent_workspace_allocation_id, runtime_secret_key)
        )
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="agent-workspace-container",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/sandboxes/ensure",
            headers=_headers(),
            json=_agent_workspace_runtime_payload(),
        )

    assert response.status_code == 200, response.text
    assert received == [("AGENT_WORKSPACE", _ENVIRONMENT_ID, "x" * 32)]


def test_controller_rejects_wrong_scope_before_docker(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/sandboxes/ensure",
            headers=_headers(),
            json=_ensure_payload(manager_scope="another-scope"),
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONTROLLER_SCOPE_MISMATCH"
    assert touched == []


def test_controller_rejects_arbitrary_docker_arguments(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        for forbidden in ("argv", "command", "mounts"):
            response = client.post(
                "/v1/sandboxes/ensure",
                headers=_headers(),
                json=_ensure_payload(**{forbidden: ["docker", "run", "--privileged"]}),
            )
            assert response.status_code == 422

    assert touched == []


def test_controller_rejects_non_deterministic_resource_name(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/sandboxes/ensure",
            headers=_headers(),
            json=_ensure_payload(backend_resource_name="victim-container"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SANDBOX_NAME_INVALID"
    assert touched == []


def test_controller_rejects_invalid_sandbox_contract_before_docker(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    invalid_payloads = (
        _ensure_payload(owner_type="SETUP_SESSION"),
        _ensure_payload(image_reference="mutable-runtime:latest"),
        _ensure_payload(
            spec={
                "workspace_relative": "../host",
                "port": 8000,
                "environment_id": _ENVIRONMENT_ID,
                "environment_version_id": _ENVIRONMENT_VERSION_ID,
                "environment_version_no": 1,
            }
        ),
        _ensure_payload(
            spec={
                "workspace_relative": "nodes/node-1",
                "port": 8000,
                "environment_id": _ENVIRONMENT_ID,
                "environment_version_id": _ENVIRONMENT_VERSION_ID,
                "environment_version_no": 1,
                "mounts": ["/:/host"],
            }
        ),
    )
    with TestClient(create_app(_settings(settings))) as client:
        for payload in invalid_payloads:
            response = client.post("/v1/sandboxes/ensure", headers=_headers(), json=payload)
            assert response.status_code == 422, response.text

    assert touched == []


def test_controller_requires_strong_key_even_in_local_mode(settings):
    configured = _settings(settings).model_copy(update={"docker_controller_api_key": "short"})

    with pytest.raises(ValueError, match="at least 32 characters"):
        create_app(configured)

    same_keys = _settings(settings).model_copy(
        update={"docker_controller_worker_api_key": _API_KEY}
    )
    with pytest.raises(ValueError, match="must be different"):
        create_app(same_keys)


def test_controller_enforces_principal_operation_boundaries(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        api_runtime = client.post(
            "/v1/sandboxes/ensure", headers=_api_headers(), json=_ensure_payload()
        )
        worker_terminal = client.post(
            "/v1/terminals/read",
            headers=_headers(),
            json={"manager_scope": _SCOPE, "terminal_id": "a" * 32},
        )

    assert api_runtime.status_code == 403
    assert api_runtime.json()["error"]["code"] == "CONTROLLER_FORBIDDEN"
    assert worker_terminal.status_code == 403
    assert worker_terminal.json()["error"]["code"] == "CONTROLLER_FORBIDDEN"
    assert touched == []


@pytest.mark.parametrize("headers", (_api_headers(), _headers()))
def test_controller_allows_owned_runtime_delete_for_api_and_worker(settings, monkeypatch, headers):
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete_expected",
        lambda self, resource_name, resource_id: deleted.append((resource_name, resource_id)),
    )
    resource_name = "fw-sbx-run-12345678-1234567890abcdef1234567890abcdef"

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/sandboxes/delete",
            headers=headers,
            json={
                "manager_scope": _SCOPE,
                "resource_name": resource_name,
                "resource_id": _RESOURCE_ID,
            },
        )

    assert response.status_code == 200, response.text
    assert deleted == [(resource_name, _RESOURCE_ID)]


def test_only_worker_can_remove_environment_credentials(settings, monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete_environment_credentials",
        lambda self, environment_id: removed.append(environment_id),
    )
    payload = {"manager_scope": _SCOPE, "environment_id": _ENVIRONMENT_ID}

    with TestClient(create_app(_settings(settings))) as client:
        denied = client.post(
            "/v1/environments/remove-credentials",
            headers=_api_headers(),
            json=payload,
        )
        allowed = client.post(
            "/v1/environments/remove-credentials",
            headers=_headers(),
            json=payload,
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "CONTROLLER_FORBIDDEN"
    assert allowed.status_code == 200
    assert removed == [_ENVIRONMENT_ID]


def test_controller_resolves_platform_setup_image_tag(settings, monkeypatch):
    resolved: list[str] = []

    def resolve_setup_image(reference: str) -> tuple[str, str]:
        environments_docker.validate_image(reference)
        resolved.append(reference)
        return reference, "sha256:" + "a" * 64

    monkeypatch.setattr(environments_docker, "resolve_setup_image", resolve_setup_image)
    payload = {
        "manager_scope": _SCOPE,
        "reference": "flowweave-openhands-runtime:1",
    }

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/environments/resolve-base-image",
            headers=_api_headers(),
            json=payload,
        )
        invalid = client.post(
            "/v1/environments/resolve-base-image",
            headers=_api_headers(),
            json={**payload, "reference": "flowweave/../host:1"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "reference": "flowweave-openhands-runtime:1",
        "digest": "sha256:" + "a" * 64,
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "ENVIRONMENT_IMAGE_INVALID"
    assert resolved == ["flowweave-openhands-runtime:1"]


@pytest.mark.parametrize(
    ("path", "allowed_role"),
    (
        ("/v1/sandboxes/inspect", "worker"),
        ("/v1/sandboxes/list", "worker"),
        ("/v1/environments/remove-image", "worker"),
        ("/v1/environments/publish", "api"),
        ("/v1/gates/execute", "worker"),
        ("/v1/dependencies/build", "worker"),
        ("/v1/plugins/resolve", "worker"),
        ("/v1/plugins/resolve-marketplace", "worker"),
        ("/v1/terminals/start", "api"),
        ("/v1/terminals/read", "api"),
        ("/v1/terminals/write", "api"),
        ("/v1/terminals/resize", "api"),
        ("/v1/terminals/close", "api"),
    ),
)
def test_controller_denies_each_single_role_operation_to_the_other_principal(
    settings, path, allowed_role
):
    headers = _headers() if allowed_role == "api" else _api_headers()

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(path, headers=headers, json={"manager_scope": _SCOPE})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONTROLLER_FORBIDDEN"


@pytest.mark.parametrize("headers", (_api_headers(), _headers()))
def test_controller_fails_closed_for_unregistered_control_path(settings, headers):
    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/future-host-control",
            headers=headers,
            json={"manager_scope": _SCOPE},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONTROLLER_FORBIDDEN"


def test_controller_allows_worker_cleanup_operations(settings, monkeypatch):
    removed_images: list[str] = []
    removed_legacy: list[str] = []
    monkeypatch.setattr(
        environments_docker,
        "remove_image",
        lambda reference, **_kwargs: removed_images.append(reference),
    )
    monkeypatch.setattr(
        environments_docker,
        "remove_legacy_setup_container",
        lambda resource_name, **_kwargs: removed_legacy.append(resource_name),
    )
    image_reference = "flowweave/environment-environment: v1-".replace(
        ": ", ":"
    ) + _ENVIRONMENT_VERSION_ID.replace("-", "")
    image_payload = {
        "manager_scope": _SCOPE,
        "reference": image_reference,
        "expected_digest": "sha256:" + "a" * 64,
        "environment_id": _ENVIRONMENT_ID,
        "version_id": _ENVIRONMENT_VERSION_ID,
        "version_no": 1,
    }
    legacy_payload = {
        "manager_scope": _SCOPE,
        "resource_name": "legacy-setup-container",
        "resource_id": "legacy",
        "environment_id": _ENVIRONMENT_ID,
    }

    with TestClient(create_app(_settings(settings))) as client:
        image_response = client.post(
            "/v1/environments/remove-image", headers=_headers(), json=image_payload
        )
        worker_legacy = client.post(
            "/v1/environments/remove-legacy", headers=_headers(), json=legacy_payload
        )
        api_legacy = client.post(
            "/v1/environments/remove-legacy", headers=_api_headers(), json=legacy_payload
        )

    assert image_response.status_code == 200, image_response.text
    assert worker_legacy.status_code == 200, worker_legacy.text
    assert api_legacy.status_code == 200, api_legacy.text
    assert removed_images == [image_reference]
    assert removed_legacy == ["legacy-setup-container", "legacy-setup-container"]


def test_controller_rejects_oversized_body_before_handler(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        DockerSandboxProvider,
        "ensure_running",
        lambda self, resource, **_kwargs: touched.append(True),
    )

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/sandboxes/ensure",
            headers={**_headers(), "Content-Type": "application/json"},
            content=b"a" * (2 * 1_048_576 + 1),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTROLLER_REQUEST_TOO_LARGE"
    assert touched == []


def test_controller_rejects_too_narrow_terminal_resize(settings):
    with TestClient(create_app(_settings(settings))) as client:
        response = client.post(
            "/v1/terminals/resize",
            headers=_api_headers(),
            json={
                "manager_scope": _SCOPE,
                "terminal_id": "a" * 32,
                "rows": 24,
                "columns": 2,
            },
        )

    assert response.status_code == 422


def test_controller_accepts_fixed_high_level_operation(settings, monkeypatch):
    observed: list[str] = []

    def ensure_running(self, resource, **_kwargs):
        observed.append(resource.id)
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="immutable-container-id",
            state="RUNNING",
            labels={
                "flowweave.managed": "true",
                "flowweave.manager-scope": _SCOPE,
                "flowweave.resource-id": resource.id,
            },
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post("/v1/sandboxes/ensure", headers=_headers(), json=_ensure_payload())

    assert response.status_code == 200
    assert response.json()["resource_identifier"] == "immutable-container-id"
    assert observed == [_RESOURCE_ID]


def test_controller_accepts_mcp_oauth_authorization_runtime(settings, monkeypatch):
    observed: list[str] = []

    def ensure_running(self, resource, **_kwargs):
        observed.append(resource.owner_type)
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="oauth-runtime-container",
            state="RUNNING",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    payload = _ensure_payload(
        owner_type="MCP_OAUTH_AUTHORIZATION",
        backend_resource_name=backend_name(
            _RESOURCE_ID,
            owner_type="MCP_OAUTH_AUTHORIZATION",
            owner_id=_OWNER_ID,
        ),
    )

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post("/v1/sandboxes/ensure", headers=_headers(), json=payload)

    assert response.status_code == 200, response.text
    assert observed == ["MCP_OAUTH_AUTHORIZATION"]


def test_controller_runtime_event_stream_requires_owned_agent_runtime(settings, monkeypatch):
    verification: dict[str, object] = {}

    def inspect_owned(
        docker_binary,
        resource_name,
        resource_id,
        *,
        expected_manager_scope,
        expected_kind=None,
        timeout,
    ):
        verification.update(
            {
                "docker_binary": docker_binary,
                "resource_name": resource_name,
                "resource_id": resource_id,
                "manager_scope": expected_manager_scope,
                "kind": expected_kind,
                "timeout": timeout,
            }
        )
        return "immutable-runtime-container-id"

    async def stream(_settings, container_id, channel, conversation_id, timeout_seconds):
        assert container_id == "immutable-runtime-container-id"
        assert channel == "CONVERSATION"
        assert conversation_id == "conversation-1"
        assert timeout_seconds == 10.0
        yield b'{"kind":"StreamingDeltaEvent","content":"hello"}\n'

    monkeypatch.setattr(controller_module, "inspect_owned_container", inspect_owned)
    monkeypatch.setattr(controller_module, "_runtime_event_stream", stream)
    payload = {
        "manager_scope": _SCOPE,
        "resource_name": "fw-sbx-12345678123442349234123456789abc",
        "resource_id": _RESOURCE_ID,
        "conversation_id": "conversation-1",
    }

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post("/v1/runtimes/events", headers=_api_headers(), json=payload)
        denied = client.post("/v1/runtimes/events", headers=_headers(), json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "kind": "StreamingDeltaEvent",
        "content": "hello",
    }
    assert verification["resource_id"] == _RESOURCE_ID
    assert verification["manager_scope"] == _SCOPE
    assert verification["kind"] == "agent-runtime"
    assert denied.status_code == 403


def test_controller_plugin_validation_is_fixed_owned_and_path_scoped(settings, monkeypatch):
    validation_id = "33333333-3333-4333-8333-333333333333"
    calls: list[dict[str, str]] = []

    def validate(
        _settings,
        *,
        resource_name: str,
        resource_id: str,
        validation_id: str,
        plugin_path: str,
    ):
        calls.append(
            {
                "resource_name": resource_name,
                "resource_id": resource_id,
                "validation_id": validation_id,
                "plugin_path": plugin_path,
            }
        )
        return {
            "plugin_name": "governed-review",
            "plugin_version": "1.0.0",
            "skill_count": 0,
            "command_count": 1,
            "agent_count": 0,
            "mcp_server_count": 0,
            "has_hooks": False,
        }

    monkeypatch.setattr(controller_module, "validate_owned_runtime_plugin", validate)
    payload = {
        "manager_scope": _SCOPE,
        "resource_name": "fw-sbx-12345678123442349234123456789abc",
        "resource_id": _RESOURCE_ID,
        "validation_id": validation_id,
        "plugin_path": (
            f"/runtime/capabilities/nodes/plugin-probe-{validation_id}/plugins/governed-review"
        ),
    }

    with TestClient(create_app(_settings(settings))) as client:
        response = client.post("/v1/runtimes/validate-plugin", headers=_api_headers(), json=payload)
        denied = client.post("/v1/runtimes/validate-plugin", headers=_headers(), json=payload)
        injected = client.post(
            "/v1/runtimes/validate-plugin",
            headers=_api_headers(),
            json={**payload, "command": ["sh", "-c", "id"]},
        )
        wrong_path = client.post(
            "/v1/runtimes/validate-plugin",
            headers=_api_headers(),
            json={
                **payload,
                "plugin_path": (
                    "/runtime/capabilities/nodes/plugin-probe-"
                    "44444444-4444-4444-8444-444444444444/plugins/governed-review"
                ),
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["plugin_name"] == "governed-review"
    assert denied.status_code == 403
    assert injected.status_code == 422
    assert wrong_path.status_code == 422
    assert wrong_path.json()["error"]["code"] == "PLUGIN_TARGET_PATH_INVALID"
    assert calls == [
        {
            "resource_name": "fw-sbx-12345678123442349234123456789abc",
            "resource_id": _RESOURCE_ID,
            "validation_id": validation_id,
            "plugin_path": (
                f"/runtime/capabilities/nodes/plugin-probe-{validation_id}/plugins/governed-review"
            ),
        }
    ]


def test_local_plugin_validation_rejects_cross_validation_path_before_docker(settings, monkeypatch):
    touched: list[bool] = []
    monkeypatch.setattr(
        docker_controller_module,
        "inspect_owned_container",
        lambda *_args, **_kwargs: touched.append(True),
    )
    configured = _settings(settings)

    with pytest.raises(DomainError) as raised:
        docker_controller_module.validate_owned_runtime_plugin(
            configured,
            resource_name="fw-sbx-12345678123442349234123456789abc",
            resource_id=_RESOURCE_ID,
            validation_id="33333333-3333-4333-8333-333333333333",
            plugin_path=(
                "/runtime/capabilities/nodes/plugin-probe-"
                "44444444-4444-4444-8444-444444444444/plugins/governed-review"
            ),
        )

    assert raised.value.code == "PLUGIN_TARGET_PATH_INVALID"
    assert touched == []
