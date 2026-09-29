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
from flowweave.runtime.read_budget import hydration_time_left
from flowweave.shared.errors import DomainError


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

    async def run_hydration(_container: object, operation: object) -> object:
        remaining = hydration_time_left()
        assert remaining is not None
        calls.append(remaining)
        return operation(object())

    if host == "workspace":
        module = workspace_router
        monkeypatch.setattr(module.conversations, "conversation_cache_key", lambda *_args: "key")
        monkeypatch.setattr(
            module.conversations, "hydrate_conversation", lambda *_args: {"ok": True}
        )
    else:
        module = node_router
        node = module.agent_sessions.flow_node_conversations
        monkeypatch.setattr(node, "node_conversation_cache_key", lambda *_args, **_kwargs: "key")
        monkeypatch.setattr(
            node, "hydrate_node_conversation", lambda *_args, **_kwargs: {"ok": True}
        )
    monkeypatch.setattr(module, "run_blocking_hydration", run_hydration)
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
    assert len(calls) == 2
    assert 0 < calls[1] <= calls[0] <= 1


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
