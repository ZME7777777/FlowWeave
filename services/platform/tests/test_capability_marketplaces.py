"""Native marketplace inheritance and loader error classification regressions."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import test_openhands as adapter_tests
from test_openhands import _handle

from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError

database = adapter_tests.database
openhands_settings = adapter_tests.openhands_settings


@pytest.mark.parametrize(
    ("loader_status", "conversation_status", "expected_code"),
    [
        (404, 200, "RUNTIME_PLUGIN_LOAD_REJECTED"),
        (404, 404, "RUNTIME_CONVERSATION_MISSING"),
        (400, None, "RUNTIME_PLUGIN_LOAD_REJECTED"),
    ],
)
def test_plugin_loader_rejection_preserves_existing_conversation(
    openhands_settings, monkeypatch, loader_status, conversation_status, expected_code
):
    runtime = OpenHandsRuntime(openhands_settings)
    path = "/api/conversations/10000000-0000-4000-8000-000000000020/load_plugin"
    calls = []

    class FakeClient:
        def request(self, method, url, **_kwargs):
            calls.append(method)
            status = loader_status if method == "POST" else conversation_status
            return httpx.Response(
                status,
                json={"detail": "protected upstream plugin detail"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(runtime, "_transport", lambda: SimpleNamespace(regular=FakeClient()))
    with pytest.raises(DomainError) as caught:
        runtime._request(
            "POST", path, base_url="http://runtime:8000", session_api_key="session-key"
        )
    assert caught.value.code == expected_code
    assert "protected upstream" not in caught.value.message
    assert calls == (["POST", "GET"] if loader_status == 404 else ["POST"])


@pytest.mark.parametrize("inherited", [False, True])
def test_dynamic_marketplace_uses_native_registration(monkeypatch, tmp_path, inherited):
    from flowweave.modules.agent_sessions.application import capability_marketplaces

    own_id = "10000000-0000-4000-8000-000000000020"
    owner_id = "10000000-0000-4000-8000-000000000021" if inherited else own_id
    root = Path("/runtime/capabilities/conversations")
    registration = {
        "name": f"flowweave-{owner_id}",
        "source": str(root / owner_id / "marketplace"),
        "auto_load": False,
    }
    runtime = SimpleNamespace(conversation_marketplaces=lambda _handle: (registration,))
    monkeypatch.setattr(capability_marketplaces, "get_runtime", lambda: runtime)
    assert capability_marketplaces.registered_marketplace_paths(
        _handle(), tmp_path / own_id, root / own_id, f"flowweave-{own_id}"
    ) == (tmp_path / owner_id, root / owner_id, f"flowweave-{owner_id}")


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "/runtime/capabilities/another-owner/marketplace"},
        {"name": "flowweave-../../another-owner"},
        {"auto_load": True},
        {"source": "/runtime/capabilities/conversations/../another-owner/marketplace"},
    ],
)
def test_dynamic_marketplace_rejects_unverified_storage(monkeypatch, tmp_path, changes):
    from flowweave.modules.agent_sessions.application import capability_marketplaces

    own_id = "10000000-0000-4000-8000-000000000020"
    root = Path("/runtime/capabilities/conversations")
    registration = {
        "name": f"flowweave-{own_id}",
        "source": str(root / own_id / "marketplace"),
        "auto_load": False,
        **changes,
    }
    runtime = SimpleNamespace(conversation_marketplaces=lambda _handle: (registration,))
    monkeypatch.setattr(capability_marketplaces, "get_runtime", lambda: runtime)
    with pytest.raises(DomainError) as caught:
        capability_marketplaces.registered_marketplace_paths(
            _handle(), tmp_path / own_id, root / own_id, f"flowweave-{own_id}"
        )
    assert caught.value.code == "AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE"
