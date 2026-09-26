from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.presentation import router as node_router
from flowweave.modules.agent_workspaces.presentation import router as workspace_router
from flowweave.modules.users.application.security import (
    Principal,
    bind_principal,
    reset_principal,
)
from flowweave.shared.errors import DomainError


class _EmptyHydrationCache:
    async def get_current_for_scope(self, _scope: object) -> None:
        return None


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

    monkeypatch.setattr(workspace_router, "run_blocking", fail_run_blocking)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            await workspace_router.agent_conversation_hydration(
                "workspace-1",
                "binding-1",
                SimpleNamespace(conversation_hydration_cache=_EmptyHydrationCache()),
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

    monkeypatch.setattr(node_router, "run_blocking", fail_run_blocking)
    token = bind_principal(Principal(user_id="user-1", username="user", role="USER"))
    try:
        with pytest.raises(DomainError) as caught:
            await node_router.node_session_hydration(
                "flow-run-1",
                "attempt-1",
                "binding-1",
                SimpleNamespace(conversation_hydration_cache=_EmptyHydrationCache()),
            )
    finally:
        reset_principal(token)

    assert caught.value.code == "AGENT_RUNTIME_UNAVAILABLE"
    assert caught.value.status == 503
    assert isinstance(caught.value.__cause__, DomainError)
    assert caught.value.__cause__.code == code
