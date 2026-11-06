"""Two-host child user surface, proof callbacks, isolation and recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

import pytest
import test_child_launcher as launcher_fixtures
from sqlalchemy import select

from flowweave.modules.agent_sessions.application import (
    child_sessions,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
)
from flowweave.modules.users.application.security import Principal, tenant_user
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeInputReadiness,
    RuntimeWorkspaceFile,
)
from flowweave.runtime.child_interaction import (
    NativeChildSnapshot,
    NativeCommandInvocation,
    NativeCommandResult,
    NativeExcerpt,
    NativeListPage,
    NativeQueryInvocation,
    NativeReadPage,
    NativeWaitResult,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import NodeAttempt, User

parent_graph = launcher_fixtures.parent_graph
launcher_http = launcher_fixtures.launcher_http
parent_profile = launcher_fixtures.parent_profile
gateway_base_url = launcher_fixtures.gateway_base_url
post_child = launcher_fixtures.post_child


@pytest.fixture
def child_http(launcher_http, monkeypatch):
    from flowweave.bootstrap import api

    g, client, _ = launcher_http
    created = post_child(g, client)
    assert created.status_code == 200, created.text
    child_id = created.json()["binding_id"]
    runtime = client.app.state.container.runtime
    container = client.app.state.container
    container.history_read_executor = container.blocking_executor
    container.history_read_slots = container.blocking_io_slots
    monkeypatch.setattr(
        api.users,
        "authenticate",
        lambda db, cookie: Principal(g.owner, g.owner, "SUPER_ADMIN", True)
        if cookie == "owner-cookie"
        else Principal(g.other, g.other, "SUPER_ADMIN", True)
        if cookie == "other-cookie"
        else None,
    )
    client.cookies.set("flowweave_session", "owner-cookie")
    g.db.commit()
    loc = child_sessions.resolve(g.db, g.binding.id, child_id)
    g.db.commit()
    runtime.read_child_preparation = lambda handle, creation: runtime.saved[creation]
    events = {}
    submissions = {}
    controls = []
    uploads = []

    def io():
        assert g.sessions.kw["bind"].pool.checkedout() == 0

    def event(handle, event_id):
        io()
        return events.get((handle.conversation_id, event_id))

    def send(handle, submission, text, images=()):
        io()
        if submission in submissions:
            assert submissions[submission][1:] == (text, images)
            return submissions[submission][0]
        event_id = str(uuid5(UUID(handle.conversation_id), f"submission:{submission}"))
        submissions[submission] = (event_id, text, images)
        events[(handle.conversation_id, event_id)] = RuntimeEvent(
            event_id, "MESSAGE", {"id": event_id, "source": "user", "content": text}
        )
        return event_id

    runtime.send_child_message = send
    runtime.read_event = event
    runtime.read_active_events = lambda handle: RuntimeEventBatch(
        events=tuple(
            value for (identity, _), value in events.items() if identity == handle.conversation_id
        ),
        cursor="native-leaf",
        readiness=RuntimeInputReadiness(True, "idle"),
        context={"event_count": 1},
    )
    runtime.input_readiness = lambda handle: RuntimeInputReadiness(True, "idle")
    runtime.conversation_context = lambda handle: {"event_count": 1, "total_tokens": 100}
    runtime.can_accept_input = lambda handle: True
    runtime.interrupt = lambda handle: controls.append((handle.conversation_id, "interrupt"))
    runtime.run = lambda handle: controls.append((handle.conversation_id, "resume"))
    runtime.condense = lambda handle: controls.append((handle.conversation_id, "condense"))
    runtime.switch_model = lambda handle, provider: controls.append(
        (handle.conversation_id, provider.model)
    )

    def upload(handle, *, filename, content_type, content, attachment_owner_id):
        io()
        assert attachment_owner_id == child_id
        path = f"{handle.workspace_root}/uploads/{attachment_owner_id}-{uuid4().hex}--{filename}"
        uploads.append((path, content))
        return path

    runtime.upload_workspace_file = upload
    runtime.download_workspace_file = lambda handle, path: RuntimeWorkspaceFile(
        "file.txt", "text/plain", b"file"
    )
    snapshot = NativeChildSnapshot(
        conversation_id=loc.handle.conversation_id,
        parent_conversation_id=loc.parent_handle.conversation_id,
        title="child",
        execution_status="finished",
        preparation_phase="started",
        checkpoint="checkpoint",
    )

    def claim(request, command=False):
        if request.token.get_secret_value() != "native-proof":
            raise DomainError("NATIVE_PROOF_DENIED", "denied", 403)
        values = dict(
            policy=g.frozen.policy,
            action_id="action",
            tool_call_id="tool-call",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return (
            NativeCommandInvocation(
                schema_version=1,
                purpose="child-command",
                audience=g.frozen.command_url,
                command=request.command,
                **values,
            )
            if command
            else NativeQueryInvocation(
                schema_version=1,
                purpose="child-query",
                audience=g.frozen.query_url,
                query=request.query,
                **values,
            )
        )

    runtime.verify_child_query = lambda handle, request: claim(request)
    runtime.verify_child_command = lambda handle, request: claim(request, True)

    def query(handle, request):
        io()
        if request.query.operation == "list":
            return NativeListPage(kind="list", items=[snapshot], complete=True)
        if request.query.operation == "read":
            return NativeReadPage(
                kind="read",
                snapshot=snapshot,
                items=[
                    NativeExcerpt(
                        event_id="formal",
                        parent_id=None,
                        kind="MessageEvent",
                        source="agent",
                        text="result",
                    )
                ],
                complete=True,
            )
        return NativeWaitResult(kind="wait", snapshot=snapshot, changed=False, timed_out=True)

    runtime.query_children = query

    def command(handle, request):
        io()
        return NativeCommandResult(
            conversation_id=loc.handle.conversation_id,
            operation=request.command.operation,
            command_id=uuid4(),
            event_id="parent-instruction",
        )

    runtime.command_child = command
    yield SimpleNamespace(
        g=g,
        client=client,
        runtime=runtime,
        loc=loc,
        events=events,
        submissions=submissions,
        controls=controls,
        uploads=uploads,
        root=f"/api/v1/agent-session-children/{g.binding.id}/{child_id}",
    )


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_message_retry_attachment_and_read_are_child_scoped(child_http):
    x = child_http
    upload = x.client.post(
        x.root + "/attachments", files={"file": ("file.txt", b"payload", "text/plain")}
    )
    assert upload.status_code == 201, upload.text
    attachment = upload.json()
    assert x.loc.binding_id in attachment["path"]
    payload = {"submission_id": str(uuid4()), "content": "do work", "attachments": [attachment]}
    first = x.client.post(x.root + "/messages", json=payload)
    assert first.status_code == 202, first.text
    assert x.client.post(x.root + "/messages", json=payload).json() == first.json()
    assert len(x.submissions) == 1
    assert x.client.get(x.root + "/file", params={"path": attachment["path"]}).content == b"file"
    hydrated = x.client.get(x.root + "/hydration")
    assert hydrated.status_code == 200, hydrated.text
    assert hydrated.json()["events"]["events"][0]["id"] == first.json()["cursor"]
    assert hydrated.json()["readiness"]["ready"] is True
    assert "do work" == hydrated.json()["events"]["events"][0]["payload"]["content"]
    assert len(hydrated.json()["events"]["events"][0]["payload"]["attachments"]) == 1
    assert x.client.get(x.root + "/context").json()["total_tokens"] == 100
    assert x.client.get(x.root).status_code == 200


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("operation", ["interrupt", "resume", "condense"])
def test_child_controls_only_target_native_child(child_http, operation):
    x = child_http
    response = x.client.post(x.root + "/control", json={"operation": operation})
    assert response.status_code == 202, response.text
    if operation == "condense":
        assert "task_id" in response.json() and not x.controls
    else:
        assert x.controls == [(x.loc.handle.conversation_id, operation)]
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize(
    "target", ["user", "parent", "workspace", "generation", "pending", "native"]
)
def test_child_surface_fails_closed_on_scope_and_gate_changes(child_http, target, monkeypatch):
    x = child_http
    if target == "user":
        x.client.cookies.set("flowweave_session", "other-cookie")
    elif target == "parent":
        x.root = x.root.replace(x.g.binding.id, str(uuid4()))
    elif target == "workspace":
        x.loc = replace(x.loc, working_directory="/other")
    elif target == "generation":
        from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspaceRuntime
        from flowweave.shared.models import FlowRunRuntime

        x.g.db.get(
            AgentWorkspaceRuntime if x.g.kind == "AGENT_WORKSPACE" else FlowRunRuntime,
            x.g.runtime_id,
        ).active_generation = 2
        x.g.db.commit()
    elif target == "pending":
        x.g.db.get(AgentConversationBinding, x.loc.binding_id).lifecycle = "PROVISIONING"
        x.g.db.commit()
    else:
        original = x.runtime.read_child_preparation
        x.runtime.read_child_preparation = lambda *args: original(*args).model_copy(
            update={"phase": "prepared"}
        )
    if target == "workspace":
        with pytest.raises(DomainError):
            child_sessions.revalidate(x.g.db, x.loc)
        return
    with tenant_user(x.g.other if target == "user" else x.g.owner):
        response = x.client.post(
            x.root + "/messages", json={"submission_id": str(uuid4()), "content": "no"}
        )
    assert response.status_code in {403, 404, 409, 503}, response.text
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_private_uploads_cannot_be_forged_or_shared(child_http):
    x = child_http
    foreign = f"{x.loc.handle.workspace_root}/uploads/{x.loc.parent_id}-{uuid4().hex}--file.txt"
    response = x.client.post(
        x.root + "/messages",
        json={"submission_id": str(uuid4()), "content": "no", "attachments": [{"path": foreign}]},
    )
    assert response.status_code in {404, 422}
    assert x.client.get(x.root + "/file", params={"path": foreign}).status_code in {404, 422}
    assert (
        x.client.get(
            x.root + "/file", params={"path": x.loc.working_directory + "/../secret"}
        ).status_code
        == 404
    )
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("operation", ["list", "read", "wait", "send", "interrupt"])
def test_parent_native_proof_query_and_command_gateway(child_http, operation):
    x = child_http
    command = operation in {"send", "interrupt"}
    url = x.g.frozen.command_url if command else x.g.frozen.query_url
    path = urlsplit(url).path.removeprefix("/prefix")
    value = {"operation": operation}
    if operation != "list":
        value["conversation_id"] = x.loc.handle.conversation_id
    if operation == "send":
        value["text"] = "parent follow-up"
    body = {"command" if command else "query": value, "token": "native-proof"}
    # A browser cookie alone is never authority for a native callback.
    assert x.client.post(path, json=body).status_code == 403
    response = x.client.post(
        path, json=body, headers={"X-Session-API-Key": x.g.key.get_secret_value()}
    )
    assert response.status_code == 200, response.text
    if not command:
        assert response.json()["kind"] == operation
    body["token"] = "forged"
    assert (
        x.client.post(
            path, json=body, headers={"X-Session-API-Key": x.g.key.get_secret_value()}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_result_reference_uses_formal_event_and_parent_only(child_http):
    x = child_http
    x.events[(x.loc.handle.conversation_id, "assistant-result")] = RuntimeEvent(
        "assistant-result", "MESSAGE", {"source": "agent", "content": "formal answer"}
    )
    result = x.client.get(x.root + "/result-reference/assistant-result")
    assert result.status_code == 200, result.text
    assert result.json()["content"] == "formal answer"
    assert not x.submissions
    assert x.client.get(x.root + "/result-reference/missing").status_code == 404
    with tenant_user(x.g.owner):
        target = x.g.db.get(AgentConversationBinding, x.loc.parent_id)
        loc = child_sessions.parent_reference_locator(x.g.db, target, x.loc.binding_id)
        x.g.db.commit()
        assert loc == x.loc


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_send_lost_ack_and_post_send_revocation_do_not_erase_native_submission(
    child_http, monkeypatch
):
    x = child_http
    original = x.runtime.send_child_message

    def lost(*args):
        original(*args)
        raise DomainError(
            "RUNTIME_CHILD_OUTCOME_UNKNOWN", "unknown", 504, {"outcome_unknown": True}
        )

    monkeypatch.setattr(x.runtime, "send_child_message", lost)
    payload = {"submission_id": str(uuid4()), "content": "same"}
    first = x.client.post(x.root + "/messages", json=payload)
    assert first.status_code == 504 and len(x.submissions) == 1
    monkeypatch.setattr(x.runtime, "send_child_message", original)
    assert x.client.post(x.root + "/messages", json=payload).status_code == 202
    assert len(x.submissions) == 1

    def revoked(*args):
        event = original(*args)
        with tenant_user(x.g.owner), x.g.sessions() as db:
            db.get(User, x.g.owner).is_active = False
            db.commit()
        return event

    monkeypatch.setattr(x.runtime, "send_child_message", revoked)
    response = x.client.post(
        x.root + "/messages", json={"submission_id": str(uuid4()), "content": "last"}
    )
    assert response.status_code == 504 and response.json()["error"]["details"]["outcome_unknown"]
    assert len(x.submissions) == 2


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("reason", ["busy", "capacity"])
def test_child_native_send_refusal_does_not_enqueue_platform_message(
    child_http, monkeypatch, reason
):
    x = child_http

    def refused(*args):
        raise DomainError("NATIVE_NOT_ADMITTED", "refused", 409 if reason == "busy" else 429)

    monkeypatch.setattr(x.runtime, "send_child_message", refused)
    response = x.client.post(
        x.root + "/messages", json={"submission_id": str(uuid4()), "content": "no"}
    )
    assert response.status_code == (409 if reason == "busy" else 429)
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("drift", ["audience", "policy", "target", "revoked-after-verify"])
def test_parent_callback_rechecks_scope_after_native_verification(child_http, monkeypatch, drift):
    x = child_http
    original = x.runtime.verify_child_command
    calls = []
    monkeypatch.setattr(x.runtime, "command_child", lambda *args: calls.append(args))

    def verifier(handle, request):
        claim = original(handle, request)
        if drift == "audience":
            return claim.model_copy(update={"audience": "https://other.example.test"})
        if drift == "policy":
            return claim.model_copy(
                update={"policy": claim.policy.model_copy(update={"generation": 2})}
            )
        if drift == "revoked-after-verify":
            with tenant_user(x.g.owner), x.g.sessions() as db:
                db.get(User, x.g.owner).is_active = False
                db.commit()
        return claim

    monkeypatch.setattr(x.runtime, "verify_child_command", verifier)
    value = {
        "operation": "interrupt",
        "conversation_id": str(uuid4()) if drift == "target" else x.loc.handle.conversation_id,
    }
    response = x.client.post(
        urlsplit(x.g.frozen.command_url).path.removeprefix("/prefix"),
        headers={"X-Session-API-Key": x.g.key.get_secret_value()},
        json={"command": value, "token": "native-proof"},
    )
    assert response.status_code in {403, 404}, response.text
    assert not calls


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_model_only_updates_child_and_stays_inside_owner_provider_policy(
    child_http, monkeypatch
):
    from flowweave.modules.agent_sessions.presentation import child_sessions as router
    from flowweave.runtime.base import RuntimeProvider

    x = child_http
    provider_id = str(uuid4())
    monkeypatch.setattr(
        router, "has_connected_default_model", lambda db, identity: identity == provider_id
    )
    monkeypatch.setattr(
        router,
        "runtime_provider",
        lambda *args, **kwargs: RuntimeProvider(
            provider_id, "https://model.example.test", "test-model", "unused"
        ),
    )
    response = x.client.post(
        x.root + "/model", json={"model_provider_id": str(uuid4()), "model_name": "test-model"}
    )
    assert response.status_code == 409 and not x.controls
    response = x.client.post(
        x.root + "/model", json={"model_provider_id": provider_id, "model_name": "test-model"}
    )
    assert response.status_code == 200, response.text
    x.g.db.expire_all()
    child = x.g.db.get(AgentConversationBinding, x.loc.binding_id)
    parent = x.g.db.get(AgentConversationBinding, x.loc.parent_id)
    assert child.model_provider_id == provider_id and parent.model_provider_id is None
    assert x.controls == [(x.loc.handle.conversation_id, "test-model")]


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_credential_sync_resolves_owner_secrets_and_records_only_on_success(
    child_http, monkeypatch
):
    from flowweave.modules.agent_sessions.infrastructure.models import (
        AgentConversationCredentialSync,
    )
    from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
    from flowweave.shared.credentials_crypto import encrypt_secret

    x = child_http
    credential = WebsiteCredential(
        name="test",
        target_host="example.test",
        auth_type="TOKEN",
        encrypted_secret=encrypt_secret("fixture-secret"),
    )
    x.g.db.add(credential)
    x.g.db.commit()
    identity = credential.id
    x.g.db.commit()
    calls = []

    def sync(handle, values, **kwargs):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        assert "fixture-secret" in values.values()
        calls.append(handle.conversation_id)

    monkeypatch.setattr(x.runtime, "update_conversation_secrets", sync, raising=False)
    result = x.client.post(x.root + "/credentials/sync", json={"credential_ids": [identity]})
    assert result.status_code == 200, result.text
    assert "fixture-secret" not in result.text
    assert calls == [x.loc.handle.conversation_id]
    synced = list(x.g.db.scalars(select(AgentConversationCredentialSync)))
    assert len(synced) == 1 and synced[0].binding_id == x.loc.binding_id
    x.g.db.commit()
    missing = x.client.post(x.root + "/credentials/sync", json={"credential_ids": [str(uuid4())]})
    assert missing.status_code == 404 and len(calls) == 1


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
def test_cancelled_flow_node_child_remains_readable_and_cannot_resume(child_http):
    x = child_http
    x.g.db.get(NodeAttempt, x.loc.scope_id).state = "CANCELLED"
    x.g.db.commit()
    assert x.client.get(x.root + "/events").status_code == 200
    assert x.client.get(x.root).json()["write_available"] is False
    assert x.client.post(x.root + "/control", json={"operation": "resume"}).status_code == 409
    assert not x.controls


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_condensation_reuses_durable_task_and_worker_child_authorization(
    child_http, monkeypatch
):
    from flowweave.modules.agent_sessions.application import condensation
    from flowweave.runtime.dependencies import runtime_context

    x = child_http
    first = x.client.post(x.root + "/control", json={"operation": "condense"})
    again = x.client.post(x.root + "/control", json={"operation": "condense"})
    assert first.status_code == 202 and again.json()["task_id"] == first.json()["task_id"]
    monkeypatch.setattr(condensation, "lease_is_current", lambda *args: True)
    x.g.db.commit()
    with runtime_context(x.runtime):
        condensation.process_manual_condensation(x.g.db, x.loc.binding_id, {}, SimpleNamespace())
    assert x.controls == [(x.loc.handle.conversation_id, "condense")]
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_results_are_resolved_for_parent_transport_and_reject_sibling_target(
    child_http, monkeypatch
):
    from flowweave.modules.agent_sessions.application import conversations, flow_node_conversations
    from flowweave.modules.agent_workspaces.infrastructure.models import AgentWorkspace
    from flowweave.runtime.dependencies import runtime_context

    x = child_http
    event_id = "formal-answer"
    x.events[(x.loc.handle.conversation_id, event_id)] = RuntimeEvent(
        event_id, "MESSAGE", {"source": "agent", "content": "server answer"}
    )
    monkeypatch.setattr(
        x.runtime,
        "child_conversation_configuration",
        lambda handle: x.runtime.configurations[handle.conversation_id],
    )
    monkeypatch.setattr(
        x.runtime,
        "read_event",
        lambda handle, identity: x.events.get((handle.conversation_id, identity)),
    )
    with runtime_context(x.runtime):
        parent = x.g.db.get(AgentConversationBinding, x.loc.parent_id)
        if x.g.kind == "AGENT_WORKSPACE":
            result = conversations._resolve_message_references(
                x.g.db,
                x.g.db.get(AgentWorkspace, parent.host_id),
                parent,
                (
                    {
                        "source_binding_id": x.loc.binding_id,
                        "source_event_id": event_id,
                        "content": "forged",
                    },
                ),
            )
            assert result[0]["content"] == "server answer"
        else:
            local, refs = flow_node_conversations.prepare_child_result_references(
                x.g.db, parent, ({"source_binding_id": x.loc.binding_id, "event_id": event_id},)
            )
            assert not local
            x.g.db.commit()
            assert (
                flow_node_conversations.read_child_result_references(refs)[0]["content"]
                == "server answer"
            )
        with pytest.raises(DomainError):
            child_sessions.parent_reference_locator(x.g.db, parent, str(uuid4()))


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_confirmation_uses_native_digest_and_never_sends_a_user_message(
    child_http, monkeypatch
):
    from flowweave.runtime.base import (
        RuntimePendingAction,
        RuntimePendingConfirmation,
        RuntimeResult,
    )

    x = child_http
    monkeypatch.setattr(
        x.runtime,
        "get_pending_confirmation",
        lambda handle: RuntimePendingConfirmation(
            "native-digest", (RuntimePendingAction("action", "call", "tool"),)
        ),
        raising=False,
    )
    observed = []

    def decision(handle, digest, accept, reason):
        observed.append((handle.conversation_id, digest, accept, reason))
        return RuntimeResult("RUNNING", cursor="formal-head")

    monkeypatch.setattr(x.runtime, "respond_to_confirmation", decision, raising=False)
    read = x.client.get(x.root + "/pending-confirmation")
    assert read.status_code == 200 and read.json()["pending_actions_digest"] == "native-digest"
    assert (
        x.client.post(
            x.root + "/pending-confirmation/decision",
            json={"expected_pending_digest": "native-digest", "accept": True, "reason": "   "},
        ).status_code
        == 422
    )
    accepted = x.client.post(
        x.root + "/pending-confirmation/decision",
        json={"expected_pending_digest": "native-digest", "accept": True, "reason": "verified"},
    )
    assert accepted.status_code == 202 and accepted.json()["cursor"] == "formal-head"
    assert observed == [(x.loc.handle.conversation_id, "native-digest", True, "verified")]
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_condensation_pending_is_separate_from_native_running(child_http):
    x = child_http
    response = x.client.post(
        x.root + "/control",
        json={"operation": "condense"},
        headers={"Idempotency-Key": "stable-condense"},
    )
    assert response.status_code == 202, response.text
    state = x.client.get(x.root + "/hydration").json()
    assert state["events"]["condensation_task"]["task_id"] == response.json()["task_id"]
    assert state["readiness"]["execution_status"] == "idle"
    assert not x.controls and not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize("parent_graph", ["AGENT_WORKSPACE"], indirect=True)
def test_child_user_surface_rejects_physical_image_drift(child_http):
    from flowweave.shared.models import ManagedSandbox

    x = child_http
    x.g.db.get(ManagedSandbox, x.loc.handle.runtime_resource_id).image_reference = (
        "sha256:" + "b" * 64
    )
    x.g.db.commit()
    response = x.client.post(
        x.root + "/messages", json={"submission_id": str(uuid4()), "content": "no"}
    )
    assert response.status_code == 404 and not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_workspace_and_annotation_context_use_authorized_sources(child_http):
    x = child_http
    event_id = str(uuid4())
    x.events[(x.loc.handle.conversation_id, event_id)] = RuntimeEvent(
        event_id, "MESSAGE", {"source": "agent", "content": "**Rendered** quote 😀"}
    )
    annotation = {
        "id": str(uuid4()),
        "anchor_kind": "CONVERSATION_TEXT",
        "anchor": {
            "source_binding_id": x.loc.binding_id,
            "event_id": event_id,
            "quote": "Rendered quote 😀",
            "compact_start": 0,
        },
        "comment": "review this selection",
    }
    workspace_reference = {
        "path": x.loc.working_directory + "/result.txt",
        "kind": "file",
        "display_name": "result.txt",
    }
    response = x.client.post(
        x.root + "/messages",
        json={
            "submission_id": str(uuid4()),
            "content": "",
            "workspace_references": [workspace_reference],
            "annotations": [annotation],
        },
    )
    assert response.status_code == 202, response.text
    prompt = next(iter(x.submissions.values()))[1]
    content, refs, workspace_refs, annotations = (
        child_sessions.conversations.project_conversation_references(prompt)
    )
    assert content == "" and refs == ()
    assert workspace_refs[0]["path"] == workspace_reference["path"]
    assert annotations[0]["anchor"] == annotation["anchor"]
    assert annotations[0]["comment"] == "review this selection"


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
@pytest.mark.parametrize(
    "invalid",
    ["sibling", "missing", "mismatched", "tool", "empty", "uuid", "workspace", "private-upload"],
)
def test_child_annotation_and_workspace_context_fail_closed(child_http, invalid):
    x = child_http
    event_id = str(uuid4())
    x.events[(x.loc.handle.conversation_id, event_id)] = RuntimeEvent(
        event_id, "MESSAGE", {"source": "agent", "content": "native quote"}
    )
    annotation = {
        "id": str(uuid4()),
        "anchor_kind": "CONVERSATION_TEXT",
        "anchor": {
            "source_binding_id": x.loc.binding_id,
            "event_id": event_id,
            "quote": "native quote",
            "compact_start": 0,
        },
        "comment": "check",
    }
    refs = []
    if invalid == "sibling":
        annotation["anchor"]["source_binding_id"] = str(uuid4())
    elif invalid == "missing":
        annotation["anchor"]["event_id"] = str(uuid4())
    elif invalid == "mismatched":
        x.events[(x.loc.handle.conversation_id, event_id)] = RuntimeEvent(
            str(uuid4()), "MESSAGE", {"content": "native quote"}
        )
    elif invalid == "tool":
        x.events[(x.loc.handle.conversation_id, event_id)] = RuntimeEvent(
            event_id, "TOOL_RESULT", {"content": "native quote"}
        )
    elif invalid == "empty":
        annotation["anchor"]["quote"] = "  "
    elif invalid == "uuid":
        annotation["id"] = "invalid"
    else:
        path = (
            "/other/result.txt"
            if invalid == "workspace"
            else x.loc.working_directory + "/uploads/another-owner-file.txt"
        )
        refs = [{"path": path, "kind": "file", "display_name": "file"}]
    response = x.client.post(
        x.root + "/messages",
        json={
            "submission_id": str(uuid4()),
            "content": "review",
            "annotations": [annotation],
            "workspace_references": refs,
        },
    )
    assert response.status_code in {404, 422}, response.text
    assert not x.submissions


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)
def test_child_credential_projection_recognizes_native_inheritance_without_claiming_versions(
    child_http, monkeypatch
):
    from flowweave.modules.agent_sessions.infrastructure.models import (
        AgentConversationCredentialSync,
    )
    from flowweave.modules.credentials.application.service import list_credentials
    from flowweave.modules.credentials.infrastructure.models import WebsiteCredential
    from flowweave.shared.credentials_crypto import encrypt_secret

    x = child_http
    for name, auth_type in (
        ("inherited", "TOKEN"),
        ("absent", "TOKEN"),
        ("partial", "USERNAME_PASSWORD"),
    ):
        x.g.db.add(
            WebsiteCredential(
                name=name,
                target_host=f"{name}.example.test",
                auth_type=auth_type,
                encrypted_username=encrypt_secret("test-user")
                if auth_type == "USERNAME_PASSWORD"
                else None,
                encrypted_secret=encrypt_secret("fixture-private-value"),
            )
        )
    x.g.db.flush()
    catalog = {entry["name"]: entry for entry in list_credentials(x.g.db)}
    x.g.db.commit()
    configuration = x.runtime.child_conversation_configuration(x.loc.handle)
    secrets = {}
    inherited_name = catalog["inherited"]["environment_names"]["token"]
    partial_name = catalog["partial"]["environment_names"]["username"]
    for name in (inherited_name, partial_name):
        secrets[name] = {"kind": "StaticSecret", "value": "**********"}
    monkeypatch.setattr(
        x.runtime,
        "child_conversation_configuration",
        lambda handle: configuration.model_copy(
            update={"secret_registry": {"secret_sources": secrets}}
        ),
    )

    response = x.client.get(x.root + "/credentials")
    assert response.status_code == 200, response.text
    state = response.json()
    entries = {entry["name"]: entry for entry in state["credentials"]}
    assert state["initialized_at"] is None
    assert entries["inherited"]["sync_state"] == "UNRECORDED"
    assert entries["inherited"]["inherited_from_binding_id"] == x.loc.parent_id
    assert "inherited_from_binding_id" not in entries["absent"]
    assert "inherited_from_binding_id" not in entries["partial"]
    assert "fixture-private-value" not in response.text
    assert "**********" not in response.text
    assert x.g.db.scalar(select(AgentConversationCredentialSync.id)) is None
    x.g.db.commit()

    def synchronize(handle, values, **kwargs):
        assert handle.conversation_id == x.loc.handle.conversation_id
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0

    monkeypatch.setattr(x.runtime, "update_conversation_secrets", synchronize, raising=False)
    synced = x.client.post(
        x.root + "/credentials/sync",
        json={"credential_ids": [catalog["inherited"]["id"]]},
    )
    assert synced.status_code == 200, synced.text
    current = x.client.get(x.root + "/credentials")
    entries = {entry["name"]: entry for entry in current.json()["credentials"]}
    assert entries["inherited"]["sync_state"] == "CURRENT"
    assert "inherited_from_binding_id" not in entries["inherited"]
    assert entries["absent"]["sync_state"] == "NEW"
