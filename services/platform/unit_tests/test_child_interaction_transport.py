"""Real fixed native wire contracts, including stable user submission admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import test_child_creation_transport as transport_fixtures
from pydantic import SecretStr

from flowweave.runtime.child_interaction import NativeCommandRequest, NativeQueryRequest
from flowweave.shared.errors import DomainError

boundary = transport_fixtures.boundary


def query_claim(handle, request):
    return {
        "schema_version": 1,
        "purpose": "child-query",
        "policy": {
            "parent_conversation_id": handle.conversation_id,
            "scope_id": str(uuid4()),
            "runtime_session_id": str(uuid4()),
            "generation": 1,
            "manifest_digest": "a" * 64,
            "max_active_children": 3,
        },
        "action_id": "action",
        "tool_call_id": "call",
        "audience": "https://gateway.example.test/queries",
        "query": request.query.model_dump(mode="json"),
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def snapshot(handle, identity):
    return {
        "conversation_id": str(identity),
        "parent_conversation_id": handle.conversation_id,
        "execution_status": "finished",
        "preparation_phase": "started",
        "checkpoint": "checkpoint",
        "leaf_event_id": "leaf",
    }


def test_stable_child_send_uses_submission_and_native_returned_identity(boundary):
    import json

    runtime, handle, _, _, requests, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    submission = uuid4()
    responses.append(httpx.Response(200, json={"success": True, "event_id": "formal-id"}))
    assert (
        runtime.send_child_message(handle, submission, "text", ("data:image/png;base64,AA==",))
        == "formal-id"
    )
    body = json.loads(requests[0].content)
    assert body["submission_id"] == str(submission) and body["run"] is True
    assert body["role"] == "user"
    assert body["content"][1]["type"] == "image"
    assert len(requests) == 1  # No full-history identity delta or guessed cursor.


@pytest.mark.parametrize("operation", ["list", "read", "wait"])
def test_query_proof_and_pagination_are_preserved(boundary, operation):
    import json

    runtime, handle, _, _, requests, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    child = uuid4()
    query = {"operation": operation}
    if operation != "list":
        query["conversation_id"] = str(child)
    if operation == "read":
        query.update(mode="results", cursor="native-cursor", max_chars=12)
    if operation == "wait":
        query.update(checkpoint="checkpoint", timeout_seconds=30)
    request = NativeQueryRequest(query=query, token=SecretStr("proof"))
    result = {"kind": operation}
    if operation == "list":
        result.update(items=[snapshot(handle, child)], complete=False, next_cursor="next")
    if operation == "read":
        result.update(
            snapshot=snapshot(handle, child),
            items=[
                {
                    "event_id": "e",
                    "parent_id": None,
                    "kind": "MessageEvent",
                    "source": "agent",
                    "text": "long result",
                    "complete": False,
                    "next_text_offset": 11,
                }
            ],
            complete=False,
            next_cursor="next",
        )
    if operation == "wait":
        result.update(snapshot=snapshot(handle, child), changed=False, timed_out=True)
    responses.extend(
        [httpx.Response(200, json=query_claim(handle, request)), httpx.Response(200, json=result)]
    )
    assert runtime.verify_child_query(handle, request).query == request.query
    page = runtime.query_children(handle, request)
    assert page.model_dump(mode="json")["kind"] == operation
    assert json.loads(requests[1].content) == request.wire_payload()
    assert requests[0].url.path.endswith("/children/queries/verify")
    assert requests[1].url.path.endswith("/children/queries")
    if operation == "read":
        assert page.next_cursor == "next" and not page.complete
        assert not page.items[0].complete


@pytest.mark.parametrize("failure", ["parent", "child", "kind", "limit", "chars"])
def test_query_response_drift_or_over_budget_is_rejected(boundary, failure):
    runtime, handle, _, _, _, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    child = uuid4()
    request = NativeQueryRequest(
        query={"operation": "read", "conversation_id": child, "limit": 1, "max_chars": 5},
        token=SecretStr("proof"),
    )
    item = {
        "event_id": "e",
        "parent_id": None,
        "kind": "MessageEvent",
        "source": "agent",
        "text": "text",
    }
    result = {
        "kind": "read",
        "snapshot": snapshot(handle, child),
        "items": [item],
        "complete": True,
    }
    if failure == "parent":
        result["snapshot"]["parent_conversation_id"] = str(uuid4())
    elif failure == "child":
        result["snapshot"]["conversation_id"] = str(uuid4())
    elif failure == "kind":
        result["kind"] = "list"
    elif failure == "limit":
        result["items"] = [item, item]
    else:
        result["items"][0]["text"] = "too long"
    responses.append(httpx.Response(200, json=result))
    with pytest.raises(DomainError):
        runtime.query_children(handle, request)


@pytest.mark.parametrize("status", [403, 409, 429])
def test_user_and_parent_native_rejection_never_retries_or_echoes_body(boundary, status):
    runtime, handle, _, _, requests, responses = boundary
    handle = replace(handle, protocol_profile="OH_FW_V4")
    responses.append(httpx.Response(status, json={"detail": "sensitive native text"}))
    with pytest.raises(DomainError) as error:
        runtime.send_child_message(handle, uuid4(), "private")
    assert error.value.status == status
    assert "sensitive native text" not in str(error.value.details)
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["query", "command", "send"])
def test_legacy_profile_refuses_all_new_interaction_before_io(boundary, operation):
    runtime, handle, _, _, requests, _ = boundary
    with pytest.raises(DomainError):
        if operation == "query":
            runtime.query_children(
                handle, NativeQueryRequest(query={"operation": "list"}, token=SecretStr("proof"))
            )
        elif operation == "command":
            runtime.command_child(
                handle,
                NativeCommandRequest(
                    command={"operation": "interrupt", "conversation_id": uuid4()},
                    token=SecretStr("proof"),
                ),
            )
        else:
            runtime.send_child_message(handle, uuid4(), "text")
    assert not requests


def test_parent_instruction_projection_preserves_formal_provenance_and_user_distinction(boundary):
    runtime, handle, _, _, _, _ = boundary
    source = {
        "operation": "send",
        "parent_conversation_id": handle.conversation_id,
        "action_id": "formal-action",
        "tool_call_id": "formal-call",
        "scope_id": str(uuid4()),
        "runtime_session_id": str(uuid4()),
        "generation": 1,
        "manifest_digest": "a" * 64,
    }
    event = {
        "kind": "MessageEvent",
        "source": "user",
        "sender": handle.conversation_id,
        "llm_message": {"role": "user", "content": [{"type": "text", "text": "instruction"}]},
        "parent_instruction": source,
    }
    projected = runtime._event_payload(event)
    assert projected["parent_instruction"] == source
    assert projected["sender"] == handle.conversation_id
    event.pop("parent_instruction")
    event.pop("sender")
    assert "parent_instruction" not in runtime._event_payload(event)
