"""Frozen family reload evidence and authorized interaction after a Runtime handoff."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import test_child_sessions as child_fixtures
from sqlalchemy import select

from flowweave.modules.agent_sessions.application import (
    child_launcher,
    child_recovery,
    child_sessions,
    usage_reconciliation,
)
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.agent_workspaces.application import service as workspace_service
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.sandboxes.application import runtime_replacement, runtime_sessions
from flowweave.modules.sandboxes.application import service as sandbox_service
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerDrainResult,
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.modules.tasks.application import handlers as task_handlers
from flowweave.modules.tasks.application import service as task_service
from flowweave.modules.tasks.public import Lease
from flowweave.modules.users.application.security import tenant_bypass, tenant_user
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeEvent
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRunRuntime, ManagedSandbox, RuntimeGeneration

parent_graph = child_fixtures.parent_graph
parent_profile = child_fixtures.parent_profile
gateway_base_url = child_fixtures.gateway_base_url
launcher_http = child_fixtures.launcher_http
child_http = child_fixtures.child_http
pytestmark = pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)


@pytest.fixture
def recovery(child_http, monkeypatch):
    x = child_http
    x.events[(x.loc.parent_handle.conversation_id, x.loc.action_id)] = RuntimeEvent(
        x.loc.action_id,
        "TOOL_CALL",
        {
            "action_id": x.loc.action_id,
            "tool_call_id": x.loc.tool_call_id,
            "parent_id": "original-user-event",
        },
    )
    x.events[(x.loc.handle.conversation_id, "original-child-answer")] = RuntimeEvent(
        "original-child-answer",
        "MESSAGE",
        {"source": "agent", "content": "持久子会话原回复", "parent_id": "original-child-task"},
    )
    calls = []

    def reload(handle, *, expected=None):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        assert handle.workspace_root == x.loc.parent_handle.workspace_root
        calls.append((handle.runtime_resource_name, handle.conversation_id, expected))
        identity = RuntimeConversationIdentity(
            handle.conversation_id,
            x.loc.working_directory,
            x.loc.persistence_dir
            if handle.conversation_id == x.loc.handle.conversation_id
            else f"/state/{handle.conversation_id.replace('-', '')}",
            "original-child-answer"
            if handle.conversation_id == x.loc.handle.conversation_id
            else "original-parent-leaf",
            "formal-parent-event",
            "formal-action-id",
            "formal-tool-call-id",
        )
        if expected is not None and identity != expected:
            raise DomainError("RUNTIME_RELOAD_IDENTITY_MISMATCH", "identity mismatch", 409)
        return identity

    x.runtime.reload_conversation = reload
    monkeypatch.setattr(child_recovery, "runtime_for", lambda *_: x.runtime)
    families = child_recovery.collect(x.g.db, x.g.runtime_id)
    x.g.db.commit()
    yield SimpleNamespace(x=x, families=families, calls=calls)


def activate_second_generation(a, monkeypatch, *, state="READY"):
    x = a.x
    db = x.g.db
    resource = ManagedSandbox(
        id=str(uuid4()),
        kind="AGENT_RUNTIME",
        owner_type="AGENT_WORKSPACE" if x.g.kind == "AGENT_WORKSPACE" else "FLOW_RUN",
        owner_id=x.loc.host_id,
        generation=2,
        backend_resource_name="fr725-target",
        image_reference=x.g.image,
        hard_expires_at=now() + timedelta(hours=1),
    )
    db.add(resource)
    db.flush()
    cls = AgentWorkspaceRuntime if x.g.kind == "AGENT_WORKSPACE" else FlowRunRuntime
    session = db.get(cls, x.g.runtime_id)
    session.active_generation = 2
    gen_cls = (
        AgentWorkspaceRuntimeGeneration if x.g.kind == "AGENT_WORKSPACE" else RuntimeGeneration
    )
    values = dict(
        runtime_session_id=session.id,
        generation=2,
        managed_runtime_id=resource.id,
        runtime_image_digest=x.g.image,
        state=state,
        fence_token=str(uuid4()),
    )
    if x.g.kind == "FLOW_NODE":
        values["protocol_profile"] = x.g.profile
        monkeypatch.setattr(
            child_launcher.sandboxes,
            "active_node_runtime_connection",
            lambda *_args, **_kwargs: SimpleNamespace(
                runtime_session_id=session.id,
                generation=2,
                resource_name=resource.backend_resource_name,
                managed_runtime_id=resource.id,
                protocol_profile=SimpleNamespace(value=x.g.profile),
            ),
        )
    generation = gen_cls(**values)
    db.add(generation)
    db.commit()
    return resource, generation


def test_handoff_probes_original_parent_child_and_audited_native_creation_action(recovery):
    a = recovery
    expected = child_recovery.probe(a.families, "source")
    restored = child_recovery.probe(a.families, "target", expected)
    assert restored == expected
    assert set(restored) == {a.x.loc.handle.conversation_id, a.x.loc.parent_handle.conversation_id}
    assert a.calls[2][2] == expected[a.x.loc.parent_handle.conversation_id]
    assert a.calls[3][2] == expected[a.x.loc.handle.conversation_id]
    assert a.x.submissions == {} and a.x.controls == []


@pytest.mark.parametrize(
    "field", ["parent", "workspace", "persistence", "receipt", "governance", "action", "event"]
)
def test_recovery_refuses_native_identity_drift_without_creating_state(
    recovery, monkeypatch, field
):
    a = recovery
    expected = child_recovery.probe(a.families, "source")
    original = a.x.runtime.child_conversation_configuration
    if field in {"parent", "workspace", "persistence"}:
        value = {
            "parent": {"parent_conversation_id": uuid4()},
            "workspace": {
                "workspace": original(a.x.loc.handle).workspace.model_copy(
                    update={"working_dir": "/other"}
                )
            },
            "persistence": {"persistence_dir": "/other"},
        }[field]
        monkeypatch.setattr(
            a.x.runtime,
            "child_conversation_configuration",
            lambda handle: original(handle).model_copy(update=value),
        )
    elif field in {"receipt", "governance"}:
        original_read = a.x.runtime.read_child_preparation

        def tamper(handle, creation):
            value = original_read(handle, creation)
            updates = (
                {"governance_id": uuid4()}
                if field == "governance"
                else {"receipt": value.receipt.model_copy(update={"request_digest": "f" * 64})}
            )
            return value.model_copy(update=updates)

        monkeypatch.setattr(a.x.runtime, "read_child_preparation", tamper)
    elif field == "action":
        a.x.events[(a.x.loc.parent_handle.conversation_id, a.x.loc.action_id)] = RuntimeEvent(
            a.x.loc.action_id,
            "TOOL_CALL",
            {"action_id": a.x.loc.action_id, "tool_call_id": "other"},
        )
    else:
        expected[a.x.loc.handle.conversation_id] = replace(
            expected[a.x.loc.handle.conversation_id], tool_call_id="other"
        )
    with pytest.raises(DomainError):
        child_recovery.probe(a.families, "target", expected)
    assert not a.x.submissions and not a.x.controls


def test_user_read_and_followup_restore_on_ready_generation_without_reauthorizing_old_tools(
    recovery, monkeypatch
):
    a = recovery
    resource, _ = activate_second_generation(a, monkeypatch)
    # Keep the relation and parent capability byte-for-byte unchanged.
    before = a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id).child_parent_manifest_json
    a.x.g.db.commit()
    response = a.x.client.get(a.x.root + "/hydration")
    assert response.status_code == 200, response.text
    assert response.json()["events"]["events"][0]["id"] == "original-child-answer"
    assert response.json()["events"]["events"][0]["payload"]["content"] == "持久子会话原回复"
    sent = a.x.client.post(
        a.x.root + "/messages", json={"submission_id": str(uuid4()), "content": "继续"}
    )
    assert sent.status_code == 202, sent.text
    a.x.g.db.expire_all()
    assert (
        a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id).child_parent_manifest_json
        == before
    )
    with pytest.raises(DomainError):
        child_sessions.revalidate(a.x.g.db, a.x.loc)
    fresh = child_sessions.resolve(a.x.g.db, a.x.loc.parent_id, a.x.loc.binding_id)
    assert fresh.generation == 2 and fresh.accepted_generation == 1
    assert fresh.handle.runtime_resource_name == resource.backend_resource_name
    assert fresh.handle.conversation_id == a.x.loc.handle.conversation_id
    a.x.g.db.commit()
    key = derive_runtime_session_key(
        a.x.g.config.openhands_session_api_key,
        a.x.g.config.sandbox_manager_scope,
        resource.backend_resource_name,
    )
    denied = a.x.client.post(
        urlsplit(a.x.g.frozen.query_url).path.removeprefix("/prefix"),
        headers={"X-Session-API-Key": key},
        json={"token": "native-proof", "query": {"operation": "list"}},
    )
    assert denied.status_code == 403, denied.text


def test_unverified_generation_never_accepts_user_interaction(recovery, monkeypatch):
    a = recovery
    activate_second_generation(a, monkeypatch, state="PROVISIONING")
    response = a.x.client.post(
        a.x.root + "/messages", json={"submission_id": str(uuid4()), "content": "继续"}
    )
    assert response.status_code == 404, response.text
    assert not a.x.submissions


def test_usage_worker_resolves_child_profile_through_its_authorized_parent(recovery, monkeypatch):
    a = recovery
    resource, _ = activate_second_generation(a, monkeypatch)
    with tenant_bypass(), tenant_user(a.x.g.other):
        handle = usage_reconciliation.resolve_handle(a.x.g.db, a.x.loc.binding_id)
    assert handle.protocol_profile == a.x.g.profile
    assert handle.runtime_resource_name == resource.backend_resource_name
    assert handle.conversation_id == a.x.loc.handle.conversation_id


def test_metadata_collection_omits_explicitly_deleting_families(recovery):
    a = recovery
    a.x.g.db.get(AgentConversationBinding, a.x.loc.parent_id).lifecycle = "DELETE_PENDING"
    a.x.g.db.flush()
    assert child_recovery.collect(a.x.g.db, a.x.g.runtime_id) == ()


def test_workspace_image_remains_frozen_across_user_contexts(recovery):
    a = recovery
    if a.x.g.kind != "AGENT_WORKSPACE":
        pytest.skip("Agent Workspace image freeze")
    with tenant_user(a.x.g.other):
        assert child_recovery.frozen_workspace_image(a.x.g.db, a.x.g.runtime_id) == a.x.g.image


@pytest.fixture
def pipeline(recovery, monkeypatch, tmp_path, request):
    a = recovery
    x = a.x
    db = x.g.db
    workspace = x.g.kind == "AGENT_WORKSPACE"
    attempt_owned = getattr(request, "param", "run") == "attempt"
    if workspace and attempt_owned:
        pytest.skip("Attempt-owned Runtime only applies to FlowNode")
    session_type = AgentWorkspaceRuntime if workspace else FlowRunRuntime
    gen_type = AgentWorkspaceRuntimeGeneration if workspace else RuntimeGeneration
    session = db.get(session_type, x.g.runtime_id)
    if workspace:
        source = db.scalar(
            select(ManagedSandbox).where(
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == x.loc.host_id,
            )
        )
        allocation = AgentWorkspaceRuntimeAllocation(
            id=session.workspace_allocation_id,
            workspace_id=x.loc.host_id,
            secret_reference_id=str(uuid4()),
            relative_root=f".agent-workspaces/{x.loc.host_id}",
        )
        db.add(allocation)
        monkeypatch.setattr(
            workspace_service, "runtime_allocation_for_agent_workspace", lambda *_: allocation
        )
        monkeypatch.setattr(workspace_service, "agent_workspace_record_path", lambda *_: tmp_path)
        monkeypatch.setattr(
            workspace_service,
            "resolve_agent_workspace_runtime_secret",
            lambda *_: "persistent-test-secret",
        )
        monkeypatch.setattr(
            workspace_service,
            "resolve_setup_image",
            lambda *_: pytest.fail("frozen image must be reused"),
        )
        source.desired_state = "DELETED"
        session.status = "RECONNECTING"
    else:
        if attempt_owned:
            session.node_attempt_id = x.loc.scope_id
            session.owner_user_id = x.g.owner
        source = ManagedSandbox(
            id=str(uuid4()),
            kind="AGENT_RUNTIME",
            owner_type="FLOW_NODE_ATTEMPT" if attempt_owned else "FLOW_RUN",
            owner_id=x.loc.scope_id if attempt_owned else x.loc.host_id,
            generation=1,
            backend_resource_name="fr725-source",
            image_reference=x.g.image,
            runtime_allocation_id=session.workspace_allocation_id,
            hard_expires_at=now() + timedelta(hours=1),
        )
        db.add(source)
        if not attempt_owned:
            monkeypatch.setattr(runtime_replacement, "_require_task_lease", lambda *_: None)
        else:
            monkeypatch.setattr(
                child_launcher.sandboxes,
                "active_node_runtime_connection",
                runtime_sessions.active_node_attempt_runtime_connection,
            )
            monkeypatch.setattr(
                runtime_replacement,
                "node_attempt_workspace_context",
                lambda *_args, **_kwargs: SimpleNamespace(
                    runtime_working_directory=x.loc.working_directory
                ),
            )
        monkeypatch.setattr(
            runtime_replacement, "resolve_runtime_secret", lambda *_: "persistent-test-secret"
        )

        def probe(*, resource_name, conversation_id, expected, workspace_root, protocol_profile):
            from flowweave.runtime.base import RuntimeHandle

            return x.runtime.reload_conversation(
                RuntimeHandle(
                    "probe",
                    conversation_id,
                    runtime_resource_name=resource_name,
                    workspace_root=workspace_root,
                    protocol_profile=protocol_profile,
                ),
                expected=expected,
            )

        monkeypatch.setattr(runtime_replacement, "_probe_identity", probe)
    source.backend_resource_id = "source-container"
    source.observed_state = "RUNNING"
    db.flush()
    values = dict(
        runtime_session_id=session.id,
        generation=1,
        managed_runtime_id=source.id,
        runtime_image_digest=x.g.image,
        state="READY",
        fence_token=str(uuid4()),
    )
    if not workspace:
        values["protocol_profile"] = x.g.profile
    db.add(gen_type(**values))
    db.commit()
    state = SimpleNamespace(ensured=[], drained=[], deleted=[], checking=True)
    source_name = source.backend_resource_name

    def ensure(_provider, resource, **_kwargs):
        assert resource.image_reference == x.g.image
        state.ensured.append((resource.id, resource.generation, resource.image_reference))
        return DockerObservation(
            resource.id,
            resource.backend_resource_name,
            f"container-{resource.generation}",
            "RUNNING",
            {},
        )

    def drain(_provider, resource):
        state.drained.append(resource.backend_resource_name)
        return DockerDrainResult(graceful=True, stopped=True)

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure)
    monkeypatch.setattr(DockerSandboxProvider, "drain", drain)
    monkeypatch.setattr(
        DockerSandboxProvider,
        "delete",
        lambda _, resource: state.deleted.append(resource.backend_resource_name),
    )
    original_config = x.runtime.child_conversation_configuration

    def guarded_config(handle):
        if state.checking and handle.runtime_resource_name != source_name:
            with x.g.sessions() as reader:
                frozen_session = reader.get(session_type, x.g.runtime_id)
                assert frozen_session.active_generation == 1 and frozen_session.status != "ACTIVE"
        return original_config(handle)

    monkeypatch.setattr(x.runtime, "child_conversation_configuration", guarded_config)
    lease = Lease(str(uuid4()), "replacement-test", 1)
    task = None
    if attempt_owned:
        runtime_replacement.enqueue_flow_run_runtime_replacement(
            db,
            flow_run_id=x.loc.host_id,
            node_attempt_id=x.loc.scope_id,
            failed_generation=1,
            reason="test-native-family",
        )
        db.commit()
        task, lease = task_service.claim(
            db,
            "replacement-test",
            lease_seconds=600,
            task_types=frozenset({"REPLACE_FLOW_RUN_RUNTIME"}),
        )
        assert task.payload_json["node_attempt_id"] == x.loc.scope_id

    def invoke():
        db.commit()
        if workspace:
            workspace_service.process_agent_workspace_runtime(db, x.loc.host_id)
        elif attempt_owned:
            task_handlers.handle(db, task, lease)
        else:
            runtime_replacement.process_flow_run_runtime_replacement(db, x.loc.host_id, 1, lease)
        db.commit()

    yield SimpleNamespace(
        a=a,
        invoke=invoke,
        state=state,
        session_type=session_type,
        gen_type=gen_type,
        source_name=source_name,
        source=source,
        task=task,
        lease=lease,
    )


@pytest.mark.parametrize("pipeline", ["run", "attempt"], indirect=True)
def test_real_control_plane_pipeline_checks_families_before_activation_and_reuses_image(pipeline):
    p = pipeline
    p.invoke()
    db = p.a.x.g.db
    db.expire_all()
    session = db.get(p.session_type, p.a.x.g.runtime_id)
    assert session.status == "ACTIVE" and session.active_generation == 2
    assert session.runtime_image_digest == p.a.x.g.image
    assert p.state.drained == [p.source_name]
    assert p.state.deleted == [p.source_name]
    assert {call[1] for call in p.a.calls} == {
        p.a.x.loc.parent_handle.conversation_id,
        p.a.x.loc.handle.conversation_id,
    }
    assert p.a.x.submissions == {} and p.a.x.controls == []


@pytest.mark.parametrize("pipeline", ["run", "attempt"], indirect=True)
def test_pipeline_failed_child_identity_keeps_original_ids_and_retries_same_generation(
    pipeline, monkeypatch
):
    p = pipeline
    original = p.a.x.runtime.child_conversation_configuration

    def mismatch(handle):
        value = original(handle)
        return (
            value.model_copy(update={"persistence_dir": "/wrong"})
            if handle.runtime_resource_name != p.source_name
            else value
        )

    monkeypatch.setattr(p.a.x.runtime, "child_conversation_configuration", mismatch)
    with pytest.raises(DomainError) as failed:
        p.invoke()
    assert failed.value.code == "AGENT_CHILD_RECOVERY_IDENTITY_MISMATCH"
    p.a.x.g.db.rollback()
    p.a.x.g.db.expire_all()
    session = p.a.x.g.db.get(p.session_type, p.a.x.g.runtime_id)
    assert session.status != "ACTIVE" and session.active_generation == 1
    assert (
        p.a.x.g.db.get(AgentConversationBinding, p.a.x.loc.binding_id).openhands_conversation_id
        == p.a.x.loc.handle.conversation_id
    )
    monkeypatch.setattr(p.a.x.runtime, "child_conversation_configuration", original)
    p.invoke()
    p.a.x.g.db.expire_all()
    session = p.a.x.g.db.get(p.session_type, p.a.x.g.runtime_id)
    assert session.status == "ACTIVE" and session.active_generation == 2
    assert {generation for _, generation, _ in p.state.ensured} == {2}
    assert len({identity for identity, _, _ in p.state.ensured}) == 1


def test_started_child_requires_formal_events_in_replacement_proof(recovery, monkeypatch):
    a = recovery
    original = a.x.runtime.reload_conversation

    def empty(handle, *, expected=None):
        identity = original(handle, expected=expected)
        return (
            replace(identity, event_id=None)
            if handle.conversation_id == a.x.loc.handle.conversation_id
            else identity
        )

    monkeypatch.setattr(a.x.runtime, "reload_conversation", empty)
    with pytest.raises(DomainError) as failed:
        child_recovery.probe(a.families, "target")
    assert failed.value.code == "AGENT_CHILD_RECOVERY_IDENTITY_MISMATCH"


def test_unmaterialized_reservation_is_preserved_without_creating_native_conversation(
    recovery, monkeypatch
):
    a = recovery
    a.x.g.db.get(AgentConversationBinding, a.x.loc.binding_id).lifecycle = "PROVISIONING"
    a.x.g.db.flush()
    families = child_recovery.collect(a.x.g.db, a.x.g.runtime_id)
    a.x.g.db.commit()

    def absent(*_):
        raise DomainError("RUNTIME_CONVERSATION_MISSING", "not materialized", 404)

    monkeypatch.setattr(a.x.runtime, "read_child_preparation", absent)
    identities = child_recovery.probe(families, "target")
    assert set(identities) == {a.x.loc.parent_handle.conversation_id}
    assert not a.x.submissions and not a.x.controls


def test_prepared_child_can_complete_original_admission_before_drain(recovery, monkeypatch):
    a = recovery
    prepared = a.x.runtime.saved[a.x.loc.creation_id]
    a.x.runtime.saved[a.x.loc.creation_id] = prepared.model_copy(update={"phase": "prepared"})
    original = a.x.runtime.reload_conversation

    def before_admission(handle, *, expected=None):
        identity = original(handle, expected=expected)
        return (
            replace(identity, event_id=None)
            if handle.conversation_id == a.x.loc.handle.conversation_id
            else identity
        )

    monkeypatch.setattr(a.x.runtime, "reload_conversation", before_admission)
    captured = child_recovery.probe(a.families, "source")
    a.x.runtime.saved[a.x.loc.creation_id] = prepared
    monkeypatch.setattr(a.x.runtime, "reload_conversation", original)
    restored = child_recovery.probe(a.families, "target", captured)
    assert restored[a.x.loc.handle.conversation_id].event_id == "original-child-answer"
    assert not a.x.submissions and not a.x.controls


@pytest.mark.parametrize("pipeline", ["run", "attempt"], indirect=True)
def test_ungraceful_family_handoff_waits_native_lease_without_holding_database(
    pipeline, monkeypatch
):
    from flowweave.modules.sandboxes.application import runtime_sessions

    p = pipeline
    slept = []

    class Clock(datetime):
        elapsed = 0.0

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=cls.elapsed)

    def advance(seconds):
        assert p.a.x.g.sessions.kw["bind"].pool.checkedout() == 0
        slept.append(seconds)
        Clock.elapsed += seconds

    monkeypatch.setattr(workspace_service, "datetime", Clock)
    monkeypatch.setattr(runtime_replacement, "datetime", Clock)
    monkeypatch.setattr(runtime_sessions, "datetime", Clock)
    monkeypatch.setattr(workspace_service.time, "sleep", advance)
    monkeypatch.setattr(
        DockerSandboxProvider, "drain", lambda *_: DockerDrainResult(graceful=False, stopped=True)
    )
    p.invoke()
    assert sum(slept) >= 44
    assert max(slept) <= 1
    p.a.x.g.db.expire_all()
    assert p.a.x.g.db.get(p.session_type, p.a.x.g.runtime_id).active_generation == 2


@pytest.mark.parametrize("parent_graph", ["AGENT_WORKSPACE"], indirect=True)
def test_default_workspace_recovery_keeps_frozen_image_even_when_current_platform_image_changes(
    pipeline, monkeypatch
):
    p = pipeline
    db = p.a.x.g.db
    workspace = db.get(AgentWorkspace, p.a.x.loc.host_id)
    allocation = db.get(
        AgentWorkspaceRuntimeAllocation,
        db.get(AgentWorkspaceRuntime, p.a.x.g.runtime_id).workspace_allocation_id,
    )
    monkeypatch.setattr(workspace_service, "_SCOPE_KEY", workspace.scope_key)
    monkeypatch.setattr(workspace_service, "_ensure_allocation", lambda *_: allocation)
    restored = workspace_service.ensure_default_agent_workspace(db)
    assert restored.id == workspace.id
    assert db.get(AgentWorkspaceRuntime, p.a.x.g.runtime_id).runtime_image_digest == p.a.x.g.image
    assert p.state.ensured == []


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize("pipeline", ["attempt"], indirect=True)
def test_attempt_access_queues_same_replacement_and_does_not_activate_before_probe(
    pipeline, monkeypatch
):
    p = pipeline
    x = p.a.x
    db = x.g.db
    session = db.get(FlowRunRuntime, x.g.runtime_id)
    session.status = "RECONNECTING"
    allocation = SimpleNamespace(
        id=session.workspace_allocation_id,
        flow_run_id=x.loc.host_id,
        node_attempt_id=x.loc.scope_id,
        relative_root=f".flow-run-runtimes/{x.loc.scope_id}",
        secret_reference_id=str(uuid4()),
    )
    environment_version_id = session.environment_version_id
    db.commit()
    monkeypatch.setattr(DockerSandboxProvider, "require_enabled", lambda *_: None)
    monkeypatch.setattr(
        sandbox_service, "runtime_allocation_for_node_attempt", lambda *_args, **_kwargs: allocation
    )
    monkeypatch.setattr(sandbox_service, "runtime_allocation_for_flow_run", lambda *_: allocation)
    monkeypatch.setattr(
        sandbox_service, "flow_run_record_id", lambda *_args, **_kwargs: x.loc.host_id
    )
    monkeypatch.setattr(sandbox_service, "resolve_runtime_secret", lambda *_: "persistent-secret")
    for _ in range(2):
        with pytest.raises(DomainError) as pending:
            sandbox_service.ensure_node_attempt_runtime(
                db,
                flow_run_id=x.loc.host_id,
                node_attempt_id=x.loc.scope_id,
                image=x.g.image,
                environment_id=str(uuid4()),
                environment_version_id=environment_version_id,
                environment_version_no=1,
                runtime_server_identity=x.g.identity,
                runtime_protocol_profile=x.g.profile,
            )
        assert pending.value.code == "RUNTIME_SESSION_NOT_ACTIVE"
        db.rollback()
    assert p.state.ensured == []
    from flowweave.shared.models import BackgroundTask

    tasks = list(
        db.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "REPLACE_FLOW_RUN_RUNTIME")
        )
    )
    assert [task.id for task in tasks] == [p.task.id]
    db.commit()
    p.invoke()
    db.expire_all()
    active = runtime_sessions.active_node_attempt_runtime_connection(
        db, flow_run_id=x.loc.host_id, node_attempt_id=x.loc.scope_id
    )
    assert active.generation == 2
    assert active.runtime_session_id == x.g.runtime_id
    db.commit()
    p.state.checking = False
    hydrated = x.client.get(x.root + "/hydration")
    assert hydrated.status_code == 200, hydrated.text
    sent = x.client.post(
        x.root + "/messages",
        json={
            "submission_id": str(uuid4()),
            "content": "恢复后继续",
        },
    )
    assert sent.status_code == 202, sent.text


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize("pipeline", ["attempt"], indirect=True)
def test_attempt_health_failure_fences_and_queues_native_family_recovery(pipeline):
    p = pipeline
    db = p.a.x.g.db
    source_id = p.source.id
    db.expunge(p.source)
    db.commit()
    for _ in range(2):
        with p.a.x.g.sessions.kw["bind"].connect() as connection:
            result = sandbox_service._apply_reconcile_outcome(
                connection,
                p.source,
                sandbox_service._ReconcileOutcome("RUNTIME_LOST"),
                now=now(),
                interval_seconds=30,
            )
        assert result == (0, 1)
    db.expire_all()
    assert db.get(FlowRunRuntime, p.a.x.g.runtime_id).status == "RECONNECTING"
    assert db.get(ManagedSandbox, source_id).desired_state == "RUNNING"
    db.commit()
    p.invoke()
    db.expire_all()
    assert db.get(FlowRunRuntime, p.a.x.g.runtime_id).active_generation == 2


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize("pipeline", ["attempt"], indirect=True)
def test_attempt_unconfirmed_stop_never_reloads_target_and_retries_same_generation(
    pipeline, monkeypatch
):
    p = pipeline
    monkeypatch.setattr(
        DockerSandboxProvider, "drain", lambda *_: DockerDrainResult(graceful=False, stopped=False)
    )
    with pytest.raises(DomainError) as pending:
        p.invoke()
    assert pending.value.code == "RUNTIME_REPLACEMENT_DRAIN_PENDING"
    assert {resource for resource, _, _ in p.a.calls} == {p.source_name}
    assert p.state.deleted == []
    monkeypatch.setattr(
        DockerSandboxProvider, "drain", lambda *_: DockerDrainResult(graceful=True, stopped=True)
    )
    p.invoke()
    assert len({identity for identity, _, _ in p.state.ensured}) == 1
    p.a.x.g.db.expire_all()
    assert p.a.x.g.db.get(FlowRunRuntime, p.a.x.g.runtime_id).active_generation == 2


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize("pipeline", ["attempt"], indirect=True)
def test_attempt_expired_task_lease_never_prewarm_or_probe(pipeline):
    p = pipeline
    p.task.lease_until = now() - timedelta(seconds=1)
    p.a.x.g.db.commit()
    with pytest.raises(RuntimeError, match="task lease was lost"):
        p.invoke()
    assert p.state.ensured == [] and p.a.calls == []
