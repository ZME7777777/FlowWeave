from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.presentation import router as node_router
from flowweave.modules.agent_workspaces.presentation import router as workspace_router
from flowweave.modules.users.application.security import (
    Principal,
    bind_principal,
    reset_principal,
)
from flowweave.runtime.base import RuntimeEventBatch, RuntimeHandle, RuntimeInputReadiness
from flowweave.runtime.read_budget import hydration_time_left
from flowweave.shared.errors import DomainError
from flowweave.shared.http import observe_hydration_phase
from flowweave.shared.observability import Metrics


@pytest.fixture(autouse=True)
def database():
    """Route contract tests do not use the database fixture."""

    yield


class _EmptyHydrationCache:
    async def get_current_for_scope(self, _scope: object) -> None:
        return None

    async def get_or_load(self, _key: object, loader: object) -> object:
        return await loader()


@pytest.mark.asyncio
async def test_hydration_phase_metrics_are_split_without_identity_labels() -> None:
    metrics = Metrics()
    container = SimpleNamespace(metrics=metrics)

    assert await observe_hydration_phase(container, "runtime_read", asyncio.sleep(0, "ok")) == "ok"

    rendered = metrics.render()
    assert 'operation="agent_session.hydration.runtime_read"' in rendered
    assert "workspace" not in rendered
    assert "binding" not in rendered


def test_workspace_prepared_hydration_runtime_read_has_no_database_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = workspace_router.conversations.PreparedConversationHydration(
        workspace_id="workspace-1",
        binding_id="binding-1",
        key=SimpleNamespace(),
        handle=RuntimeHandle(job_id="job", conversation_id="conversation"),
    )
    calls: list[RuntimeHandle] = []

    class Runtime:
        def reload_conversation(self, handle: RuntimeHandle) -> None:
            calls.append(handle)

        def read_active_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
            calls.append(handle)
            return RuntimeEventBatch(
                cursor="event-1",
                context={"usage_current": False},
                readiness=RuntimeInputReadiness(ready=True, execution_status="idle"),
            )

        def input_readiness(self, _handle: RuntimeHandle) -> RuntimeInputReadiness:
            raise AssertionError("active batch readiness must be reused")

    monkeypatch.setattr(workspace_router.conversations, "get_runtime", lambda: Runtime())

    batch, context, readiness = workspace_router.conversations.read_prepared_conversation_hydration(
        prepared
    )

    assert calls == [prepared.handle, prepared.handle]
    assert batch.cursor == "event-1"
    assert context == {"usage_current": False}
    assert readiness == {"ready": True, "execution_status": "idle"}


@pytest.mark.parametrize("host", ["workspace", "node"])
def test_hydration_projection_rejects_runtime_identity_drift(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    key = SimpleNamespace(runtime_session_id="runtime-1", conversation_id="conversation-1")
    prepared_handle = RuntimeHandle(
        job_id="job", conversation_id="conversation-1", runtime_resource_id="generation-1"
    )
    current_handle = RuntimeHandle(
        job_id="job", conversation_id="conversation-1", runtime_resource_id="generation-2"
    )
    binding = SimpleNamespace(
        runtime_session_id="runtime-1", openhands_conversation_id="conversation-1"
    )

    if host == "workspace":
        module = workspace_router.conversations
        prepared = module.PreparedConversationHydration(
            workspace_id="workspace-1",
            binding_id="binding-1",
            key=key,
            handle=prepared_handle,
        )
        monkeypatch.setattr(module, "_workspace", lambda *_args: object())
        monkeypatch.setattr(module, "_binding", lambda *_args: binding)
        monkeypatch.setattr(module, "_handle", lambda *_args: current_handle)
        check = module._assert_prepared_conversation_hydration_current
    else:
        module = node_router.agent_sessions.flow_node_conversations
        prepared = module.PreparedNodeConversationHydration(
            flow_run_id="run-1",
            attempt_id="attempt-1",
            binding_id="binding-1",
            key=key,
            handle=prepared_handle,
        )
        monkeypatch.setattr(module, "_binding_for_attempt", lambda *_args, **_kwargs: binding)
        monkeypatch.setattr(module, "_flow_run_handle", lambda *_args: current_handle)
        check = module._assert_prepared_node_conversation_hydration_current

    with pytest.raises(DomainError) as caught:
        check(object(), prepared)

    assert caught.value.code == "AGENT_RUNTIME_REPLACEMENT_FENCED"


def test_node_prepared_hydration_runtime_read_has_no_database_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = node_router.agent_sessions.flow_node_conversations.PreparedNodeConversationHydration(
        flow_run_id="run-1",
        attempt_id="attempt-1",
        binding_id="binding-1",
        key=SimpleNamespace(),
        handle=RuntimeHandle(job_id="job", conversation_id="conversation"),
    )
    calls: list[RuntimeHandle] = []

    class Runtime:
        def read_active_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
            calls.append(handle)
            return RuntimeEventBatch(
                cursor="event-1",
                context={"usage_current": False},
                readiness=RuntimeInputReadiness(ready=True, execution_status="idle"),
            )

        def input_readiness(self, _handle: RuntimeHandle) -> RuntimeInputReadiness:
            raise AssertionError("active batch readiness must be reused")

    node = node_router.agent_sessions.flow_node_conversations
    monkeypatch.setattr(node, "get_runtime", lambda: Runtime())

    batch, context, readiness = node.read_prepared_node_conversation_hydration(prepared)

    assert calls == [prepared.handle]
    assert batch.cursor == "event-1"
    assert context == {"usage_current": False}
    assert readiness == {"ready": True, "execution_status": "idle"}


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["workspace", "node"])
async def test_hydration_normalizes_runtime_phase_failures(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    module = workspace_router if host == "workspace" else node_router
    prepared = SimpleNamespace(key="key")

    async def prepare(_container: object, operation: object) -> object:
        return operation(object())

    async def fail_runtime(*_args: object, **_kwargs: object) -> object:
        raise DomainError("RUNTIME_READ_PER_RUNTIME_SATURATED", "formal read failed", 503)

    if host == "workspace":
        monkeypatch.setattr(
            module.conversations, "prepare_conversation_hydration", lambda *_args: prepared
        )
    else:
        node = module.agent_sessions.flow_node_conversations
        monkeypatch.setattr(
            node, "prepare_node_conversation_hydration", lambda *_args, **_kwargs: prepared
        )
    monkeypatch.setattr(module, "run_blocking_hydration", prepare)
    monkeypatch.setattr(module, "run_hydration_runtime", fail_runtime)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            if host == "workspace":
                await module.agent_conversation_hydration(
                    "workspace-1",
                    "binding-1",
                    SimpleNamespace(
                        conversation_hydration_cache=_EmptyHydrationCache(),
                        settings=SimpleNamespace(hydration_read_timeout_seconds=1),
                    ),
                )
            else:
                await module.node_session_hydration(
                    "run-1",
                    "attempt-1",
                    "binding-1",
                    SimpleNamespace(
                        conversation_hydration_cache=_EmptyHydrationCache(),
                        settings=SimpleNamespace(hydration_read_timeout_seconds=1),
                    ),
                )
    finally:
        reset_principal(token)

    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert isinstance(caught.value.__cause__, DomainError)
    assert caught.value.__cause__.code == "RUNTIME_READ_PER_RUNTIME_SATURATED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["RUNTIME_READ_PER_RUNTIME_SATURATED", "RUNTIME_BUSINESS_READ_TIMEOUT"],
)
async def test_workspace_hydration_normalizes_formal_runtime_read_failures(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    async def fail_run_blocking(*_args: object, **_kwargs: object) -> object:
        raise DomainError(code, "formal read failed", 503)

    monkeypatch.setattr(workspace_router, "run_blocking_hydration", fail_run_blocking)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            await workspace_router.agent_conversation_hydration(
                "workspace-1",
                "binding-1",
                SimpleNamespace(
                    conversation_hydration_cache=_EmptyHydrationCache(),
                    settings=SimpleNamespace(hydration_read_timeout_seconds=10),
                ),
            )
    finally:
        reset_principal(token)

    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert caught.value.status == 503
    assert isinstance(caught.value.__cause__, DomainError)
    assert caught.value.__cause__.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["RUNTIME_READ_PER_RUNTIME_SATURATED", "RUNTIME_BUSINESS_READ_TIMEOUT"],
)
async def test_node_hydration_normalizes_formal_runtime_read_failures(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    async def fail_run_blocking(*_args: object, **_kwargs: object) -> object:
        raise DomainError(code, "formal read failed", 503)

    monkeypatch.setattr(node_router, "run_blocking_hydration", fail_run_blocking)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            await node_router.node_session_hydration(
                "flow-run-1",
                "attempt-1",
                "binding-1",
                SimpleNamespace(
                    conversation_hydration_cache=_EmptyHydrationCache(),
                    settings=SimpleNamespace(hydration_read_timeout_seconds=10),
                ),
            )
    finally:
        reset_principal(token)

    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert caught.value.status == 503
    assert isinstance(caught.value.__cause__, DomainError)
    assert caught.value.__cause__.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["workspace", "node"])
async def test_both_hydration_hosts_use_reserved_lane_and_one_budget(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    calls: list[float] = []
    phases: list[str] = []
    prepared = SimpleNamespace(key="key")

    async def run_hydration(_container: object, operation: object) -> object:
        remaining = hydration_time_left()
        assert remaining is not None
        calls.append(remaining)
        return operation(object())

    async def run_runtime(_container: object, operation: object) -> object:
        remaining = hydration_time_left()
        assert remaining is not None
        calls.append(remaining)
        return operation()

    if host == "workspace":
        module = workspace_router
        monkeypatch.setattr(
            module.conversations,
            "prepare_conversation_hydration",
            lambda *_args: phases.append("prepare") or prepared,
        )
        monkeypatch.setattr(
            module.conversations,
            "read_prepared_conversation_hydration",
            lambda value: phases.append("runtime") or {"prepared": value},
        )
        monkeypatch.setattr(
            module.conversations,
            "project_prepared_conversation_hydration",
            lambda _db, value, snapshot: phases.append("project")
            or {"ok": value is prepared and snapshot == {"prepared": prepared}},
        )
    else:
        module = node_router
        node = module.agent_sessions.flow_node_conversations
        monkeypatch.setattr(
            node,
            "prepare_node_conversation_hydration",
            lambda *_args, **_kwargs: phases.append("prepare") or prepared,
        )
        monkeypatch.setattr(
            node,
            "read_prepared_node_conversation_hydration",
            lambda value: phases.append("runtime") or {"prepared": value},
        )
        monkeypatch.setattr(
            node,
            "project_prepared_node_conversation_hydration",
            lambda _db, value, snapshot: phases.append("project")
            or {"ok": value is prepared and snapshot == {"prepared": prepared}},
        )
    monkeypatch.setattr(module, "run_blocking_hydration", run_hydration)
    monkeypatch.setattr(module, "run_hydration_runtime", run_runtime)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    container = SimpleNamespace(
        conversation_hydration_cache=_EmptyHydrationCache(),
        settings=SimpleNamespace(hydration_read_timeout_seconds=1),
    )
    try:
        if host == "workspace":
            result = await module.agent_conversation_hydration(
                "workspace-1", "binding-1", container
            )
        else:
            result = await module.node_session_hydration(
                "flow-run-1", "attempt-1", "binding-1", container
            )
    finally:
        reset_principal(token)
    assert result == {"ok": True}
    assert phases == ["prepare", "runtime", "project"]
    assert len(calls) == 3
    assert 0 < calls[2] <= calls[1] <= calls[0] <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["workspace", "node"])
async def test_both_hydration_hosts_end_response_after_budget(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    module = workspace_router if host == "workspace" else node_router

    async def stalled_key_lookup(*_args: object) -> object:
        await asyncio.sleep(1)
        return "never"

    monkeypatch.setattr(module, "run_blocking_hydration", stalled_key_lookup)
    container = SimpleNamespace(
        conversation_hydration_cache=_EmptyHydrationCache(),
        settings=SimpleNamespace(hydration_read_timeout_seconds=0.02),
    )
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            if host == "workspace":
                await module.agent_conversation_hydration("workspace-1", "binding-1", container)
            else:
                await module.node_session_hydration(
                    "flow-run-1", "attempt-1", "binding-1", container
                )
    finally:
        reset_principal(token)
    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert isinstance(caught.value.__cause__, DomainError)
    assert caught.value.__cause__.code == "RUNTIME_BUSINESS_READ_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["workspace", "node"])
@pytest.mark.parametrize(
    ("cursor", "history_cursor", "expected_history"),
    [(None, None, False), ("event-1", "older-1", False), (None, "older-1", True)],
)
async def test_event_hosts_share_deadline_and_preserve_history_lane(
    monkeypatch, host, cursor, history_cursor, expected_history
):
    from flowweave.shared import http

    module = workspace_router if host == "workspace" else node_router
    calls: list[bool] = []

    async def interactive(_container, _operation):
        calls.append(False)
        assert 0 < hydration_time_left() <= 0.02
        await asyncio.sleep(1)

    async def history(_container, _operation):
        calls.append(True)
        assert 0 < hydration_time_left() <= 0.02
        await asyncio.sleep(1)

    monkeypatch.setattr(http, "run_blocking", interactive)
    monkeypatch.setattr(http, "run_blocking_history", history)
    container = SimpleNamespace(settings=SimpleNamespace(runtime_event_read_timeout_seconds=0.02))
    with pytest.raises(DomainError) as caught:
        if host == "workspace":
            await module.agent_events(
                "workspace",
                "binding",
                container,
                cursor=cursor,
                history_cursor=history_cursor,
                diagnostic_trigger=None,
            )
        else:
            await module.node_session_events(
                "run",
                "attempt",
                "binding",
                container,
                cursor=cursor,
                history_cursor=history_cursor,
                diagnostic_trigger=None,
            )
    assert calls == [expected_history]
    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert caught.value.__cause__.code == "RUNTIME_BUSINESS_READ_TIMEOUT"


@pytest.mark.asyncio
async def test_nested_response_budget_respects_shorter_parent():
    from flowweave.runtime.read_budget import formal_response_budget, hydration_response_budget

    before = asyncio.get_running_loop().time()
    with pytest.raises(DomainError) as caught:
        async with hydration_response_budget(0.02):
            async with formal_response_budget(1):
                await asyncio.sleep(1)
    assert caught.value.code == "RUNTIME_BUSINESS_READ_TIMEOUT"
    assert asyncio.get_running_loop().time() - before < 0.5
