from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application import credential_sync
from flowweave.modules.credentials.application.service import credential_secret_descriptions
from flowweave.runtime.base import RuntimeHandle
from flowweave.shared.errors import DomainError


def summary():
    return {
        "id": "credential",
        "name": "example",
        "target_host": "example.test",
        "target_path": "/admin",
        "include_subdomains": False,
        "auth_type": "USERNAME_PASSWORD",
        "row_version": 3,
        "secret_hint": "NEVER_PUBLISH_HINT",
        "environment_names": {"username": "CONTROLLED_USERNAME", "password": "CONTROLLED_PASSWORD"},
    }


def test_secret_descriptions_publish_scope_and_command_mapping_only():
    descriptions = credential_secret_descriptions([summary()])
    assert set(descriptions) == {"CONTROLLED_USERNAME", "CONTROLLED_PASSWORD"}
    for text in descriptions.values():
        assert '"target_host": "example.test"' in text
        assert '"target_path": "/admin"' in text
        assert '"host_scope": "exact"' in text
        assert "CONTROLLED_USERNAME" in text and "CONTROLLED_PASSWORD" in text
        assert "显式引用" in text and "ES_QUERY_USERNAME" in text
        assert "NEVER_PUBLISH_HINT" not in text


@pytest.mark.parametrize("failure", [False, True])
def test_sync_records_metadata_only_after_runtime_accepts_it(monkeypatch, failure):
    calls = []
    item = summary()
    monkeypatch.setattr(
        credential_sync,
        "resolve_credentials_for_agent",
        lambda db, ids: (
            {"CONTROLLED_USERNAME": "private-user", "CONTROLLED_PASSWORD": "private-password"},
            "unused-startup-suffix",
            [item],
        ),
    )

    def update(handle, values, *, descriptions):
        assert values["CONTROLLED_USERNAME"] == "private-user"
        assert "example.test" in descriptions["CONTROLLED_USERNAME"]
        assert "private-user" not in str(descriptions)
        calls.append("runtime")
        if failure:
            raise DomainError("AGENT_CREDENTIAL_CONTEXT_UNSUPPORTED", "unsupported", 409)

    monkeypatch.setattr(credential_sync, "_record_versions", lambda *args: calls.append("ledger"))
    monkeypatch.setattr(credential_sync, "list_credential_sync_state", lambda *args: {"ok": True})
    args = (
        SimpleNamespace(),
        SimpleNamespace(id="binding"),
        RuntimeHandle(job_id="job", conversation_id="native"),
        SimpleNamespace(update_conversation_secrets=update),
        ("credential",),
    )
    if failure:
        with pytest.raises(DomainError):
            credential_sync.synchronize_credentials(*args)
        assert calls == ["runtime"]
    else:
        assert credential_sync.synchronize_credentials(*args) == {"ok": True}
        assert calls == ["runtime", "ledger"]


def test_old_value_only_sync_remains_selectable_for_metadata_repair(monkeypatch):
    monkeypatch.setattr(credential_sync, "list_credentials", lambda db: [summary()])
    monkeypatch.setattr(
        credential_sync,
        "_sync_rows",
        lambda db, binding: {
            "credential": SimpleNamespace(credential_row_version=3, context_version=0)
        },
    )
    binding = SimpleNamespace(id="binding", credential_sync_initialized_at=None)
    state = credential_sync.list_credential_sync_state(SimpleNamespace(), binding)
    assert state["credentials"][0]["sync_state"] == "UNRECORDED"
    binding.credential_sync_initialized_at = datetime.now(UTC)
    state = credential_sync.list_credential_sync_state(SimpleNamespace(), binding)
    assert state["credentials"][0]["sync_state"] == "MODIFIED"
