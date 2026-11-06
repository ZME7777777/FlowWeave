"""Native child HTTP boundary; no platform execution or authorization simulation."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.child_creation import (
    NativeChildConfiguration,
    NativeChildLaunchRequest,
    verify_child_configuration,
)
from flowweave.runtime.child_governance import (
    NativeChildPreparationOverrides,
    NativeChildStaticSecret,
)
from flowweave.runtime.mock import MockRuntime
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    runtime = OpenHandsRuntime(Settings(workspace_root=tmp_path))
    handle = RuntimeHandle(
        "agent-workspace:test",
        str(uuid4()),
        runtime_resource_name="native-test",
        protocol_profile="OH_FW_V3",
        workspace_root="/workspace",
    )
    request = NativeChildLaunchRequest(
        task="审查文件 🌳", title="review", token=SecretStr("opaque-sensitive-token")
    )
    creation = uuid4()
    requests = []
    responses = []

    def serve(incoming):
        requests.append(incoming)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.Client(transport=httpx.MockTransport(serve))
    monkeypatch.setattr(
        runtime, "_transport", lambda: SimpleNamespace(regular=client, control=client)
    )
    try:
        yield runtime, handle, request, creation, requests, responses
    finally:
        client.close()
        asyncio.run(runtime.aclose())


def preparation(handle, creation, request, *, phase="prepared"):
    return {
        "receipt": {
            "schema_version": 1,
            "identity": {
                "parent_conversation_id": handle.conversation_id,
                "creation_id": str(creation),
                "conversation_id": str(
                    uuid5(
                        UUID(handle.conversation_id), f"openhands-child-creation-v1:{creation.hex}"
                    )
                ),
            },
            "request_digest": request.request_digest(),
            "created_at": datetime.now(UTC).isoformat(),
        },
        "phase": phase,
        "workspace": handle.workspace_root,
    }


def invocation(handle, request):
    return {
        "schema_version": 1,
        "operation": "start",
        "policy": {
            "parent_conversation_id": handle.conversation_id,
            "scope_id": str(uuid4()),
            "runtime_session_id": str(uuid4()),
            "generation": 2,
            "manifest_digest": "c" * 64,
            "max_active_children": 3,
        },
        "action_id": "action",
        "tool_call_id": "tool-call",
        "audience": "https://launcher.example.test/children",
        "request_digest": request.request_digest(),
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": datetime.now(UTC).isoformat(),
    }


def test_verifier_is_typed_evidence_without_inventing_creation_identity(boundary):
    runtime, handle, request, _, requests, responses = boundary
    responses.append(httpx.Response(200, json=invocation(handle, request)))
    result = runtime.verify_child_launcher(handle, request)
    assert result.policy.parent_conversation_id == UUID(handle.conversation_id)
    assert not hasattr(result, "creation_id")
    assert requests[0].url.path.endswith("/children/launcher/verify")
    assert json.loads(requests[0].content) == request.verifier_payload()
    assert requests[0].headers["X-Session-API-Key"] == runtime._session_key_for_handle(handle)
    assert request.task not in repr(request)
    assert request.token.get_secret_value() not in repr(request)


def resolution(handle, request):
    native = invocation(handle, request)
    creation = uuid5(
        UUID(handle.conversation_id),
        f"openhands-child-invocation-v1:{native['action_id']}:{native['tool_call_id']}",
    )
    return {
        "schema_version": 1,
        "invocation": native,
        "identity": preparation(handle, creation, request)["receipt"]["identity"],
    }


def test_v4_resolve_governed_prepare_read_release_use_native_fields(boundary):
    runtime, handle, request, _, requests, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    resolved = resolution(handle, request)
    creation = UUID(resolved["identity"]["creation_id"])
    overrides = NativeChildPreparationOverrides(
        governance_id=uuid4(),
        remove_secrets=frozenset({"PARENT"}),
        replace_secrets={"BOUND": NativeChildStaticSecret(value=SecretStr("child-private"))},
        remove_mcp_servers=frozenset({"history"}),
    )
    prepared = {
        **preparation(handle, creation, request),
        "governance_id": str(overrides.governance_id),
    }
    responses.extend(
        [
            httpx.Response(200, json=resolved),
            httpx.Response(201, json=prepared),
            httpx.Response(200, json=prepared),
            httpx.Response(200, json={**prepared, "phase": "started"}),
        ]
    )
    identity = runtime.resolve_child_launcher(handle, request).identity
    assert identity.creation_id == creation
    result = runtime.prepare_governed_child_conversation(handle, creation, request, overrides)
    assert len(requests) == 2  # no automatic release or run
    assert result.governance_id == overrides.governance_id
    assert runtime.read_child_preparation(handle, creation) == result
    assert runtime.release_child_conversation(handle, creation, request).phase == "started"
    assert json.loads(requests[1].content) == {
        **request.preparation_payload(),
        "overrides": overrides.wire_payload(),
    }
    assert requests[1].url.path.endswith("/governed")
    assert "child-private" not in repr(overrides)
    assert "child-private" not in overrides.model_dump_json()


@pytest.mark.parametrize("profile", [None, "OH_FW_V1", "OH_FW_V2", "OH_FW_V3"])
def test_v4_operations_refuse_other_profiles_before_http(boundary, profile):
    runtime, handle, request, creation, requests, _ = boundary
    handle = replace(handle, protocol_profile=profile)
    for operation in (
        lambda: runtime.resolve_child_launcher(handle, request),
        lambda: runtime.prepare_governed_child_conversation(
            handle, creation, request, NativeChildPreparationOverrides(governance_id=uuid4())
        ),
    ):
        with pytest.raises(DomainError, match="当前冻结"):
            operation()
    assert not requests


@pytest.mark.parametrize("mutation", ["parent", "creation", "digest", "schema"])
def test_v4_rejects_substituted_resolution(boundary, mutation):
    runtime, handle, request, _, _, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    value = resolution(handle, request)
    if mutation == "parent":
        value["invocation"]["policy"]["parent_conversation_id"] = str(uuid4())
    elif mutation == "creation":
        other = preparation(handle, uuid4(), request)
        value["identity"] = other["receipt"]["identity"]
    elif mutation == "digest":
        value["invocation"]["request_digest"] = "d" * 64
    else:
        value["schema_version"] = 2
    responses.append(httpx.Response(200, json=value))
    with pytest.raises(DomainError) as rejected:
        runtime.resolve_child_launcher(handle, request)
    assert rejected.value.code == "RUNTIME_CHILD_PROTOCOL_INVALID"
    assert request.task not in str(rejected.value)


@pytest.mark.parametrize("governance", [None, "other"])
def test_v4_prepare_requires_the_returned_governance_identity(boundary, governance):
    runtime, handle, request, creation, _, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    value = preparation(handle, creation, request)
    if governance:
        value["governance_id"] = str(uuid4())
    responses.append(httpx.Response(201, json=value))
    with pytest.raises(DomainError) as rejected:
        runtime.prepare_governed_child_conversation(
            handle, creation, request, NativeChildPreparationOverrides(governance_id=uuid4())
        )
    assert rejected.value.details["outcome_unknown"] is True


def test_prepare_reconcile_release_use_exact_identity_and_do_not_auto_dispatch(boundary):
    runtime, handle, request, creation, requests, responses = boundary
    prepared = preparation(handle, creation, request)
    responses.extend(
        [
            httpx.Response(201, json=prepared),
            httpx.Response(200, json=prepared),
            httpx.Response(200, json=preparation(handle, creation, request, phase="started")),
        ]
    )
    first = runtime.prepare_child_conversation(handle, creation, request)
    assert len(requests) == 1  # prepare must never call release or /run
    assert runtime.read_child_preparation(handle, creation) == first
    released = runtime.release_child_conversation(handle, creation, request)
    assert released.phase == "started"
    assert [r.method for r in requests] == ["POST", "GET", "POST"]
    root = f"/api/conversations/{handle.conversation_id}/children/preparations/{creation}"
    assert [r.url.path for r in requests] == [root, root, root + "/release"]
    assert json.loads(requests[0].content) == json.loads(requests[2].content)
    assert json.loads(requests[0].content) == request.preparation_payload()
    assert not requests[1].content


@pytest.mark.parametrize("operation", ["verify", "prepare", "read", "release", "config"])
@pytest.mark.parametrize("profile", [None, "OH_FW_V1", "OH_FW_V2"])
def test_old_profiles_refuse_before_http(boundary, operation, profile):
    runtime, handle, request, creation, requests, _ = boundary
    handle = replace(handle, protocol_profile=profile)
    with pytest.raises(DomainError) as caught:
        call(runtime, operation, handle, creation, request)
    assert caught.value.code == "RUNTIME_CHILD_UNSUPPORTED"
    assert not requests


def call(runtime, operation, handle, creation, request):
    return {
        "verify": lambda: runtime.verify_child_launcher(handle, request),
        "prepare": lambda: runtime.prepare_child_conversation(handle, creation, request),
        "read": lambda: runtime.read_child_preparation(handle, creation),
        "release": lambda: runtime.release_child_conversation(handle, creation, request),
        "config": lambda: runtime.child_conversation_configuration(handle),
    }[operation]()


@pytest.mark.parametrize("operation", ["verify", "prepare", "read", "release", "config"])
def test_mock_refuses_native_feature(boundary, operation):
    _, handle, request, creation, _, _ = boundary
    with pytest.raises(DomainError) as caught:
        call(MockRuntime(), operation, handle, creation, request)
    assert caught.value.code == "RUNTIME_CHILD_UNSUPPORTED"


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500])
@pytest.mark.parametrize("operation", ["verify", "prepare", "read", "release", "config"])
def test_native_rejection_has_no_followup_or_secret_echo(boundary, status, operation):
    runtime, handle, request, creation, requests, responses = boundary
    responses.append(httpx.Response(status, text=request.token.get_secret_value() + request.task))
    if operation == "read" and status == 404:
        assert call(runtime, operation, handle, creation, request) is None
    else:
        with pytest.raises(DomainError) as caught:
            call(runtime, operation, handle, creation, request)
        assert caught.value.status == (status if status < 500 else 502)
        assert caught.value.details == {"status_code": status, "outcome_unknown": status >= 500}
        assert request.task not in str(caught.value)
        assert request.token.get_secret_value() not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["prepare", "release"])
@pytest.mark.parametrize("failure", ["timeout", "disconnect", "invalid-json", "wrong-shape"])
def test_lost_or_invalid_mutation_response_remains_unknown(boundary, operation, failure):
    runtime, handle, request, creation, requests, responses = boundary
    responses.append(
        {
            "timeout": httpx.ReadTimeout("lost"),
            "disconnect": httpx.RemoteProtocolError("lost"),
            "invalid-json": httpx.Response(200, text="invalid"),
            "wrong-shape": httpx.Response(200, json={"token": request.token.get_secret_value()}),
        }[failure]
    )
    with pytest.raises(DomainError) as caught:
        call(runtime, operation, handle, creation, request)
    assert caught.value.details["outcome_unknown"] is True
    assert request.token.get_secret_value() not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["prepare", "read", "release"])
@pytest.mark.parametrize("forgery", ["parent", "creation", "child", "phase", "missing-marker"])
def test_malformed_receipts_never_become_absence_or_valid_evidence(boundary, operation, forgery):
    runtime, handle, request, creation, _, responses = boundary
    value = preparation(handle, creation, request)
    if forgery in {"parent", "creation", "child"}:
        key = {
            "parent": "parent_conversation_id",
            "creation": "creation_id",
            "child": "conversation_id",
        }[forgery]
        value["receipt"]["identity"][key] = str(uuid4())
    elif forgery == "phase":
        value["phase"] = "finished"
    else:
        value = {"_flowweave_missing": True}
    responses.append(httpx.Response(200, json=value))
    with pytest.raises(DomainError) as caught:
        call(runtime, operation, handle, creation, request)
    assert caught.value.code == "RUNTIME_CHILD_PROTOCOL_INVALID"


@pytest.mark.parametrize("field", ["parent", "digest"])
def test_verifier_rejects_wrong_parent_or_task_digest(boundary, field):
    runtime, handle, request, _, _, responses = boundary
    value = invocation(handle, request)
    if field == "parent":
        value["policy"]["parent_conversation_id"] = str(uuid4())
    else:
        value["request_digest"] = "0" * 64
    responses.append(httpx.Response(200, json=value))
    with pytest.raises(DomainError) as caught:
        runtime.verify_child_launcher(handle, request)
    assert caught.value.code == "RUNTIME_CHILD_PROTOCOL_INVALID"


def test_configuration_includes_full_skills_and_compares_public_native_document(boundary):
    runtime, handle, request, creation, requests, responses = boundary
    identity = runtime._child_preparation_response(
        handle, creation, preparation(handle, creation, request)
    ).receipt.identity
    child_handle = replace(handle, conversation_id=str(identity.conversation_id))
    document = {
        "id": child_handle.conversation_id,
        "parent_conversation_id": handle.conversation_id,
        "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspace"},
        "persistence_dir": "/state",
        "agent": {"kind": "Agent", "tools": [], "agent_context": {"skills": [{"name": "test"}]}},
    }
    responses.append(httpx.Response(200, json=document))
    result = runtime.child_conversation_configuration(child_handle)
    assert requests[0].url.params["include_skills"] == "true"
    verify_child_configuration(
        result, expected=identity, working_directory="/workspace", expected_agent=document["agent"]
    )
    for field in ("id", "parent_conversation_id", "workspace", "agent"):
        changed = deepcopy(document)
        changed[field] = {
            "workspace": {"working_dir": "/elsewhere"},
            "agent": {"kind": "Other"},
        }.get(field, str(uuid4()))
        with pytest.raises(DomainError) as caught:
            verify_child_configuration(
                NativeChildConfiguration.model_validate(changed),
                expected=identity,
                working_directory="/workspace",
                expected_agent=document["agent"],
            )
        assert caught.value.code == "AGENT_CHILD_NATIVE_IDENTITY_MISMATCH"


def test_request_disallows_worktree_and_model_selected_scope():
    for extra in ({"isolation": "worktree"}, {"scope_id": str(uuid4())}):
        with pytest.raises(ValidationError):
            NativeChildLaunchRequest(task="task", token=SecretStr("opaque"), **extra)


@pytest.mark.parametrize("operation", ["prepare", "read", "release"])
@pytest.mark.parametrize("swapped", ["parent", "creation"])
def test_other_well_formed_native_receipt_cannot_be_substituted(boundary, operation, swapped):
    runtime, handle, request, creation, _, responses = boundary
    other_handle = replace(handle, conversation_id=str(uuid4())) if swapped == "parent" else handle
    other_creation = uuid4() if swapped == "creation" else creation
    responses.append(httpx.Response(200, json=preparation(other_handle, other_creation, request)))
    with pytest.raises(DomainError) as caught:
        call(runtime, operation, handle, creation, request)
    assert caught.value.code == "RUNTIME_CHILD_PROTOCOL_INVALID"


@pytest.mark.parametrize("operation", ["prepare", "release"])
def test_receipt_binds_exact_task_digest(boundary, operation):
    runtime, handle, request, creation, _, responses = boundary
    value = preparation(handle, creation, request)
    value["receipt"]["request_digest"] = "0" * 64
    responses.append(httpx.Response(200, json=value))
    with pytest.raises(DomainError) as caught:
        call(runtime, operation, handle, creation, request)
    assert caught.value.details["outcome_unknown"] is True


def test_configuration_rejects_another_native_id(boundary):
    runtime, handle, _, _, _, responses = boundary
    responses.append(
        httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "parent_conversation_id": str(uuid4()),
                "workspace": {"working_dir": "/workspace"},
                "persistence_dir": None,
                "agent": {},
            },
        )
    )
    with pytest.raises(DomainError) as caught:
        runtime.child_conversation_configuration(handle)
    assert caught.value.code == "RUNTIME_CHILD_PROTOCOL_INVALID"


def test_v4_parent_configuration_accepts_root_and_rejects_wrong_identity(boundary):
    runtime, handle, _, _, requests, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    value = {
        "id": handle.conversation_id,
        "parent_conversation_id": None,
        "workspace": {"working_dir": handle.workspace_root},
        "persistence_dir": "/state/parent",
        "agent": {"kind": "Agent", "tools": []},
    }
    responses.extend(
        [httpx.Response(200, json=value), httpx.Response(200, json={**value, "id": str(uuid4())})]
    )
    assert runtime.parent_conversation_configuration(handle).parent_conversation_id is None
    assert requests[0].url.params["include_skills"] == "true"
    with pytest.raises(DomainError, match="冻结契约"):
        runtime.parent_conversation_configuration(handle)
    with pytest.raises(DomainError):
        runtime.parent_conversation_configuration(replace(handle, protocol_profile="OH_FW_V3"))
    assert len(requests) == 2
