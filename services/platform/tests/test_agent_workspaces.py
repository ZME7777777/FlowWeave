from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from flowweave.bootstrap.runtime_provider import RuntimeProviderResourceWrite
from flowweave.modules.agent_workspaces.application import conversations
from flowweave.modules.agent_workspaces.application.service import (
    ensure_default_agent_workspace,
    process_agent_workspace_runtime,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspaceRuntime,
    AgentWorkspaceRuntimeAllocation,
    AgentWorkspaceRuntimeGeneration,
)
from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
)
from flowweave.runtime.base import (
    RuntimeConversationIdentity,
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimePendingAction,
    RuntimePendingConfirmation,
    RuntimeProvider,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import BackgroundTask, ManagedSandbox, TaskState
from flowweave.shared.settings import settings_context


def test_default_agent_workspace_has_external_storage_and_no_flow_owner(
    settings, db_session_factory
):
    with settings_context(settings), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        allocation = db.scalar(
            select(AgentWorkspaceRuntimeAllocation).where(
                AgentWorkspaceRuntimeAllocation.workspace_id == workspace.id
            )
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert allocation is not None
        assert runtime is not None
        assert allocation.relative_root == ".agent-workspaces/platform-default"
        assert (settings.workspace_root / allocation.relative_root / "workspace/project").is_dir()
        assert (settings.workspace_root / allocation.relative_root / "state/conversations").is_dir()
        assert runtime.runtime_image_digest == "sha256:" + "0" * 64
        db.commit()


def test_unavailable_workspace_runtime_refreshes_missing_deployment_image(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"runtime_adapter": "openhands"})
    first_digest = "sha256:" + "1" * 64
    replacement_digest = "sha256:" + "2" * 64
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
        lambda _reference: ("image", first_digest),
    )
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        assert runtime is not None and runtime.runtime_image_digest == first_digest
        monkeypatch.setattr(
            "flowweave.modules.agent_workspaces.application.service.resolve_setup_image",
            lambda _reference: ("image", replacement_digest),
        )
        runtime.active_generation = 1
        runtime.status = "RECONNECTING"
        ensure_default_agent_workspace(db)
        assert runtime.runtime_image_digest == replacement_digest
        task = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == workspace.id))
        assert task is not None
        task.state = TaskState.DEAD
        task.attempts = task.max_attempts
        task.last_error = "previous deployment image was unavailable"
        ensure_default_agent_workspace(db)
        assert task.state == TaskState.RETRY
        assert task.attempts == 0
        assert task.last_error is None


def test_workspace_runtime_replaces_deleted_physical_generation(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"terminal_environment_backend": "docker"})

    def ensure_running(_self, resource, *, runtime_secret_key):
        assert resource.owner_type == "AGENT_WORKSPACE"
        assert runtime_secret_key is not None
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier=f"container-{resource.generation}",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    monkeypatch.setattr(DockerSandboxProvider, "delete", lambda *_args, **_kwargs: None)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        process_agent_workspace_runtime(db, workspace.id)
        first = db.scalar(
            select(ManagedSandbox).where(
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == workspace.id,
            )
        )
        assert first is not None
        assert first.generation == 1
        first.desired_state = "DELETED"
        first.observed_state = "ERROR"
        first.next_reconcile_at = datetime.now(UTC)
        db.flush()

        process_agent_workspace_runtime(db, workspace.id)
        current = db.scalar(
            select(ManagedSandbox)
            .where(
                ManagedSandbox.owner_type == "AGENT_WORKSPACE",
                ManagedSandbox.owner_id == workspace.id,
            )
            .order_by(ManagedSandbox.generation.desc())
        )
        runtime = db.scalar(
            select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
        )
        generations = list(
            db.scalars(
                select(AgentWorkspaceRuntimeGeneration).order_by(
                    AgentWorkspaceRuntimeGeneration.generation
                )
            )
        )
        assert current is not None
        assert current.generation == 2
        assert runtime is not None and runtime.active_generation == 2
        assert [item.generation for item in generations] == [1, 2]


def test_agent_workspace_runtime_spec_matches_runtime_provider_contract(
    settings, db_session_factory, monkeypatch
):
    configured = settings.model_copy(update={"terminal_environment_backend": "docker"})
    captured: list[ManagedSandbox] = []

    def ensure_running(_self, resource, *, runtime_secret_key):
        captured.append(resource)
        assert runtime_secret_key is not None
        return DockerObservation(
            resource_id=resource.id,
            resource_name=resource.backend_resource_name,
            resource_identifier="agent-workspace-container",
            state="READY",
            labels={},
        )

    monkeypatch.setattr(DockerSandboxProvider, "ensure_running", ensure_running)
    with settings_context(configured), db_session_factory() as db:
        workspace = ensure_default_agent_workspace(db)
        process_agent_workspace_runtime(db, workspace.id)
        assert len(captured) == 1
        resource = captured[0]
        payload = RuntimeProviderResourceWrite.model_validate(
            {
                "manager_scope": configured.sandbox_manager_scope,
                "id": resource.id,
                "kind": resource.kind,
                "owner_type": resource.owner_type,
                "owner_id": resource.owner_id,
                "backend_resource_name": resource.backend_resource_name,
                "image_reference": resource.image_reference,
                "spec": resource.spec_json,
                "created_at": resource.created_at,
                "runtime_secret_key": "x" * 32,
            }
        )
        assert str(payload.spec.runtime_allocation_id) == resource.agent_workspace_allocation_id


def _ready_workspace_for_conversation(db):
    workspace = ensure_default_agent_workspace(db)
    provider = ModelProvider(
        name=f"agent-workspace-provider-{workspace.id}",
        base_url="https://models.example.test/v1",
        connection_state="CONNECTED",
    )
    db.add(provider)
    db.flush()
    db.add(
        ProviderModel(
            provider_id=provider.id,
            model_name="test-model",
            enabled=True,
            is_default=True,
        )
    )
    workspace.default_model_provider_id = provider.id
    runtime = db.scalar(
        select(AgentWorkspaceRuntime).where(AgentWorkspaceRuntime.workspace_id == workspace.id)
    )
    assert runtime is not None
    runtime.status = "ACTIVE"
    runtime.active_generation = 1
    db.add(
        ManagedSandbox(
            kind="AGENT_RUNTIME",
            owner_type="AGENT_WORKSPACE",
            owner_id=workspace.id,
            backend_resource_name=f"agent-workspace-{workspace.id}",
            image_reference=runtime.runtime_image_digest,
            agent_workspace_allocation_id=runtime.workspace_allocation_id,
            hard_expires_at=datetime.max.replace(tzinfo=UTC),
            observed_state="READY",
        )
    )
    db.flush()
    return workspace


def test_agent_workspace_conversation_create_is_idempotent_and_uses_external_identity(
    settings, db_session_factory, monkeypatch
):
    class CapturingRuntime(MockRuntime):
        request = None

        def create_conversation(self, request):
            self.request = request
            return super().create_conversation(request)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = CapturingRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        first = conversations.create_conversation(
            db, workspace.id, "第一会话", workspace.default_model_provider_id, "create-key"
        )
        replay = conversations.create_conversation(
            db, workspace.id, "ignored", workspace.default_model_provider_id, "create-key"
        )

        assert replay == first
        assert first["lifecycle"] == "ACTIVE"
        assert first["display_title"] == "第一会话"
        assert len(conversations.list_conversations(db, workspace.id)) == 1
        assert runtime.request is not None
        assert runtime.request.agent_spec.confirmation_policy == "NEVER"
        assert runtime.request.agent_spec.condenser.kind == "LLM_SUMMARIZING"
        assert runtime.request.agent_spec.condenser_provider is not None


def test_agent_workspace_message_failure_is_ambiguous_and_delete_is_tombstoned(
    settings, db_session_factory, monkeypatch
):
    class FailingRuntime(MockRuntime):
        def send_message(self, handle, content, image_urls=()):
            del handle, content, image_urls
            raise DomainError("EXECUTOR_UNAVAILABLE", "upstream unavailable", 503)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    with settings_context(settings), db_session_factory() as db, runtime_context(FailingRuntime()):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        try:
            conversations.message(db, workspace.id, created["id"], "hello")
        except DomainError as exc:
            assert exc.code == "AGENT_MESSAGE_DELIVERY_AMBIGUOUS"
            assert exc.status == 504
        else:
            raise AssertionError("message transport failure must not be retried")

        conversations.delete_conversation(db, workspace.id, created["id"], "delete-key")
        assert conversations.list_conversations(db, workspace.id) == []


def test_agent_workspace_uses_native_attachments_context_and_model_switch(
    settings, db_session_factory, monkeypatch
):
    class NativeWorkspaceRuntime(MockRuntime):
        sent: tuple[str, tuple[str, ...]] | None = None
        switched: RuntimeProvider | None = None

        def upload_workspace_file(self, handle, *, filename, content_type, content):
            del handle, content_type, content
            return f"/runtime/workspace/project/uploads/{'a' * 32}-{filename}"

        def send_message(self, handle, content, image_urls=()):
            self.sent = (content, image_urls)
            return super().send_message(handle, content, image_urls)

        def conversation_context(self, handle):
            del handle
            return {
                "used_tokens": None,
                "window_tokens": 128_000,
                "cumulative_tokens": 456,
                "model_name": "test-model",
                "reasoning_effort": "medium",
            }

        def switch_model(self, handle, provider):
            del handle
            self.switched = provider

    resolved_provider_ids: list[str] = []

    def resolve_provider(_db, asset, model_name=None, reasoning_effort=None):
        provider_id = asset["asset"]["executor"]["model_provider_id"]
        resolved_provider_ids.append(provider_id)
        return RuntimeProvider(
            provider_id=provider_id,
            base_url="https://models.example.test/v1",
            model=model_name or "test-model",
            api_key="x",
            reasoning_effort=reasoning_effort,
        )

    monkeypatch.setattr(conversations, "runtime_provider", resolve_provider)
    runtime = NativeWorkspaceRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        assert created["model_provider_id"] == workspace.default_model_provider_id
        attachment = conversations.upload_attachment(
            db,
            workspace.id,
            created["id"],
            filename="diagram.png",
            content_type="image/png",
            content=b"image-bytes",
        )
        conversations.message(
            db,
            workspace.id,
            created["id"],
            "请分析图片",
            (
                {
                    "path": str(attachment["path"]),
                    "image_data_url": str(attachment["image_data_url"]),
                },
            ),
        )
        assert runtime.sent == (
            "请分析图片\n\n已上传到共享工作区的附件：\n"
            f"- /runtime/workspace/project/uploads/{'a' * 32}-diagram.png",
            ("data:image/png;base64,aW1hZ2UtYnl0ZXM=",),
        )
        conversations.message(
            db,
            workspace.id,
            created["id"],
            "切换后发送",
            model_name="test-model-2",
            reasoning_effort="high",
        )
        assert runtime.switched is not None
        assert runtime.switched.model == "test-model-2"
        assert runtime.switched.reasoning_effort == "high"
        assert conversations.conversation_context(db, workspace.id, created["id"]) == {
            "used_tokens": None,
            "window_tokens": 128_000,
            "cumulative_tokens": 456,
            "model_name": "test-model",
            "reasoning_effort": "medium",
        }
        # A provider is bound to the individual Conversation.  Staging a new
        # provider is committed atomically with its next formal user message,
        # not through the workspace's legacy default field.
        original_provider_id = workspace.default_model_provider_id
        replacement_provider = ModelProvider(
            name=f"replacement-provider-{workspace.id}",
            base_url="https://replacement.example.test/v1",
            connection_state="CONNECTED",
        )
        db.add(replacement_provider)
        db.flush()
        db.add(
            ProviderModel(
                provider_id=replacement_provider.id,
                model_name="replacement-model",
                enabled=True,
                is_default=True,
            )
        )
        workspace.default_model_provider_id = original_provider_id
        conversations.message(
            db,
            workspace.id,
            created["id"],
            "切换供应商后发送",
            model_provider_id=replacement_provider.id,
            model_name="replacement-model",
        )
        assert runtime.switched is not None
        assert runtime.switched.model == "replacement-model"
        assert resolved_provider_ids[-1] == replacement_provider.id
        assert (
            conversations.get_conversation(db, workspace.id, created["id"])["model_provider_id"]
            == replacement_provider.id
        )


def test_agent_workspace_reapplies_bound_provider_after_runtime_reload_before_send(
    settings, db_session_factory, monkeypatch
):
    class RebindingRuntime(MockRuntime):
        active_provider_id = "provider-from-persisted-create"
        switched: list[str] = []
        sent: list[str] = []
        fail_switch = False
        fail_context = False

        def conversation_context(self, handle):
            del handle
            if self.fail_context:
                raise DomainError("EXECUTOR_UNAVAILABLE", "context failed", 503)
            return {
                "used_tokens": None,
                "window_tokens": None,
                "cumulative_tokens": None,
                "provider_id": self.active_provider_id,
                "model_name": "persisted-model",
                "reasoning_effort": None,
            }

        def switch_model(self, handle, provider):
            del handle
            if self.fail_switch:
                raise DomainError("EXECUTOR_UNAVAILABLE", "switch failed", 503)
            self.active_provider_id = provider.provider_id
            self.switched.append(provider.provider_id)

        def send_message(self, handle, content, image_urls=()):
            self.sent.append(content)
            return super().send_message(handle, content, image_urls)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda _db, asset, **_kwargs: RuntimeProvider(
            provider_id=asset["asset"]["executor"]["model_provider_id"],
            base_url="https://models.example.test/v1",
            model="bound-model",
            api_key="x",
        ),
    )
    runtime = RebindingRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        bound_provider_id = str(created["model_provider_id"])

        conversations.message(db, workspace.id, created["id"], "reload 后发送")

        assert runtime.switched == [bound_provider_id]
        assert runtime.sent == ["reload 后发送"]

        runtime.fail_switch = False
        runtime.fail_context = True
        try:
            conversations.message(db, workspace.id, created["id"], "仍不得发送")
        except DomainError as exc:
            assert exc.code == "AGENT_MODEL_REBIND_FAILED"
            assert exc.status == 503
        else:
            raise AssertionError("provider identity read failure must stop the user event")
        assert runtime.sent == ["reload 后发送"]

        runtime.active_provider_id = "provider-from-persisted-create"
        runtime.fail_context = False
        runtime.fail_switch = True
        try:
            conversations.message(db, workspace.id, created["id"], "不得发送")
        except DomainError as exc:
            assert exc.code == "AGENT_MODEL_REBIND_FAILED"
            assert exc.status == 503
        else:
            raise AssertionError("provider rebind failure must stop the user event")
        assert runtime.sent == ["reload 后发送"]


def test_agent_workspace_forks_at_native_event_and_condenses_manually(
    settings, db_session_factory, monkeypatch
):
    class ForkRuntime(MockRuntime):
        fork_call: tuple[str | None, str] | None = None
        condensed = False

        def reload_conversation(self, handle, *, expected=None):
            del expected
            return RuntimeConversationIdentity(
                conversation_id=handle.conversation_id,
                workspace_working_dir="/runtime/workspace/project",
                persistence_dir=(
                    f"/runtime/state/conversations/{handle.conversation_id.replace('-', '')}"
                ),
                event_id="assistant-event",
                parent_id=None,
                action_id=None,
                tool_call_id=None,
            )

        def fork_conversation(self, handle, **kwargs):
            self.fork_call = (kwargs["from_event_id"], kwargs["expected_source_leaf_event_id"])
            return super().fork_conversation(handle, **kwargs)

        def condense(self, handle):
            self.condensed = True
            return super().condense(handle)

        def can_accept_input(self, handle):
            del handle
            return True

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = ForkRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        source = conversations.create_conversation(
            db, workspace.id, "源会话", workspace.default_model_provider_id, "create-key"
        )
        fork = conversations.fork_conversation(
            db, workspace.id, source["id"], "assistant-event", None, "fork-key"
        )
        assert fork["lifecycle"] == "ACTIVE"
        assert fork["display_title"] == "Fork · 源会话"
        assert fork["model_provider_id"] == source["model_provider_id"]
        assert runtime.fork_call == ("assistant-event", "assistant-event")
        assert (
            conversations.fork_conversation(
                db, workspace.id, source["id"], "assistant-event", None, "fork-key"
            )
            == fork
        )
        assert len(conversations.list_conversations(db, workspace.id)) == 2
        condensed = conversations.condense_conversation(db, workspace.id, source["id"])
        assert condensed["accepted"] is True
        assert runtime.condensed is True


def test_agent_workspace_blocks_resend_until_native_interrupt_has_settled(
    settings, db_session_factory, monkeypatch
):
    class SerialRuntime(MockRuntime):
        ready = True
        sent: list[str] = []

        def can_accept_input(self, handle):
            del handle
            return self.ready

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            assert self.ready
            self.sent.append(content)
            self.ready = False
            return super().send_message(handle, content)

        def interrupt(self, handle):
            del handle
            self.ready = False

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = SerialRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        conversations.message(db, workspace.id, created["id"], "first")
        conversations.interrupt(db, workspace.id, created["id"])

        try:
            conversations.message(db, workspace.id, created["id"], "second")
        except DomainError as exc:
            assert exc.code == "AGENT_CONVERSATION_BUSY"
            assert exc.status == 409
        else:
            raise AssertionError("a second message cannot be sent before interrupt settles")
        assert runtime.sent == ["first"]
        assert conversations.input_readiness(db, workspace.id, created["id"]) == {"ready": False}

        runtime.ready = True
        conversations.message(db, workspace.id, created["id"], "second")
        assert runtime.sent == ["first", "second"]


def test_agent_workspace_confirmation_uses_native_batch_digest(
    settings, db_session_factory, monkeypatch
):
    class ConfirmationRuntime(MockRuntime):
        decision: tuple[str, bool, str] | None = None

        def get_pending_confirmation(self, handle):
            del handle
            return RuntimePendingConfirmation(
                pending_actions_digest="batch-digest",
                cursor="action-event",
                actions=(
                    RuntimePendingAction(
                        action_id="action-event",
                        tool_call_id="tool-call",
                        tool_name="terminal",
                        arguments={"command": "pwd"},
                        security_risk="LOW",
                        summary="查看工作目录",
                        digest="action-digest",
                    ),
                ),
            )

        def respond_to_confirmation(self, handle, expected_pending_digest, accept, reason):
            if expected_pending_digest != "batch-digest":
                raise DomainError(
                    "RUNTIME_CONFIRMATION_DRIFTED",
                    "pending confirmation changed",
                    409,
                )
            self.decision = (expected_pending_digest, accept, reason)
            return super().respond_to_confirmation(handle, expected_pending_digest, accept, reason)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = ConfirmationRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        pending = conversations.pending_confirmation(db, workspace.id, created["id"])
        assert pending == {
            "pending": True,
            "pending_actions_digest": "batch-digest",
            "cursor": "action-event",
            "actions": [
                {
                    "action_id": "action-event",
                    "tool_call_id": "tool-call",
                    "tool_name": "terminal",
                    "arguments": {"command": "pwd"},
                    "security_risk": "LOW",
                    "summary": "查看工作目录",
                    "digest": "action-digest",
                }
            ],
        }
        result = conversations.decide_confirmation(
            db,
            workspace.id,
            created["id"],
            expected_pending_digest="batch-digest",
            accept=False,
            reason=" 不需要执行 ",
        )
        assert result["accepted"] is True
        assert runtime.decision == ("batch-digest", False, "不需要执行")
        try:
            conversations.decide_confirmation(
                db,
                workspace.id,
                created["id"],
                expected_pending_digest="stale-digest",
                accept=True,
                reason="批准",
            )
        except DomainError as exc:
            assert exc.code == "RUNTIME_CONFIRMATION_DRIFTED"
            assert exc.status == 409
        else:
            raise AssertionError("a stale confirmation digest must fail closed")


def test_agent_workspace_rewrites_only_the_active_branch_last_user_message(
    settings, db_session_factory, monkeypatch
):
    class RewriteRuntime(MockRuntime):
        calls: list[tuple[str, str | None]] = []

        def read_events(self, handle):
            del handle
            return RuntimeEventBatch(
                events=(
                    RuntimeEvent(
                        cursor="user-event",
                        event_type="MESSAGE",
                        payload={"source": "user", "content": "before", "parent_id": "root-event"},
                    ),
                )
            )

        def navigate(self, handle, event_id):
            del handle
            self.calls.append(("navigate", event_id))

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            self.calls.append(("send", content))
            return super().send_message(handle, content)

    monkeypatch.setattr(
        conversations,
        "runtime_provider",
        lambda *_args, **_kwargs: RuntimeProvider(
            provider_id="provider",
            base_url="https://models.example.test/v1",
            model="test-model",
            api_key="x",
        ),
    )
    runtime = RewriteRuntime()
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "create-key"
        )
        result = conversations.rewrite_message(
            db, workspace.id, created["id"], "user-event", "after"
        )
        assert result["accepted"] is True
        assert runtime.calls == [("navigate", "root-event"), ("send", "after")]
