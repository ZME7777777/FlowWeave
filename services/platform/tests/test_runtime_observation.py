from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowweave.modules.orchestration.application import service
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database():
    """Routing checks isolate SQL/provider boundaries; the separate probe uses PostgreSQL."""
    yield


@pytest.mark.parametrize("attempt_owned", [False, True])
def test_native_observation_validates_existing_reference_and_keeps_provider_fence(
    monkeypatch,
    attempt_owned,
):
    attempt = SimpleNamespace(id="attempt", node_run_id="node-run")
    session = SimpleNamespace(
        flow_run_id="run",
        environment_version_id="version",
        runtime_image_digest="sha256:frozen",
        protocol_profile="OH_FW_V2",
    )
    db = Mock()
    db.scalar.return_value = session
    monkeypatch.setattr(service, "_node_run", lambda *_: SimpleNamespace(flow_run_id="run"))
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(runtime_adapter="openhands")
    )
    monkeypatch.setattr(
        service.sandboxes,
        "node_attempt_workspace_context",
        lambda *_, **__: SimpleNamespace(attempt_owned=attempt_owned),
    )
    monkeypatch.setattr(service.sandboxes, "runtime_owner_flow_run_id", lambda *_: "run")
    environment = SimpleNamespace(
        id="version",
        environment_id="environment",
        version_no=1,
        manifest_json={},
        runtime_protocol_profile="OH_FW_V2",
    )
    read = Mock(return_value=environment)
    monkeypatch.setattr(service, "read_referenced_version", read)
    creation_lock = Mock(side_effect=AssertionError("Observation must not create a new reference"))
    monkeypatch.setattr(service, "lock_referenceable_version", creation_lock)
    identity = object()
    monkeypatch.setattr(service, "runtime_server_identity", lambda *_, **__: identity)
    ensure_run, ensure_attempt = Mock(), Mock()
    monkeypatch.setattr(service.sandboxes, "ensure_flow_run_runtime", ensure_run)
    monkeypatch.setattr(service.sandboxes, "ensure_node_attempt_runtime", ensure_attempt)

    service._ensure_attempt_runtime_for_native_observation(db, attempt)
    read.assert_called_once_with(
        db,
        flow_run_id="run",
        version_id="version",
        image_digest="sha256:frozen",
        protocol_profile="OH_FW_V2",
    )
    called, unused = (ensure_attempt, ensure_run) if attempt_owned else (ensure_run, ensure_attempt)
    unused.assert_not_called()
    creation_lock.assert_not_called()
    assert called.call_args.kwargs["runtime_server_identity"] is identity
    assert called.call_args.kwargs["image"] == session.runtime_image_digest
    assert called.call_args.kwargs["environment_version_id"] == session.environment_version_id
    if attempt_owned:
        assert called.call_args.kwargs["node_attempt_id"] == attempt.id


@pytest.mark.parametrize("missing", ["session", "reference"])
def test_native_observation_rejects_missing_immutable_binding_before_provider(monkeypatch, missing):
    attempt = SimpleNamespace(id="attempt", node_run_id="node")
    db = Mock()
    db.scalar.return_value = (
        None
        if missing == "session"
        else SimpleNamespace(
            flow_run_id="run",
            environment_version_id="version",
            runtime_image_digest="sha256:frozen",
            protocol_profile="OH_FW_V2",
        )
    )
    monkeypatch.setattr(service, "_node_run", lambda *_: SimpleNamespace(flow_run_id="run"))
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(runtime_adapter="openhands")
    )
    monkeypatch.setattr(
        service.sandboxes,
        "node_attempt_workspace_context",
        lambda *_, **__: SimpleNamespace(attempt_owned=False),
    )
    monkeypatch.setattr(service.sandboxes, "runtime_owner_flow_run_id", lambda *_: "run")
    monkeypatch.setattr(service, "read_referenced_version", lambda *_, **__: None)
    ensure = Mock()
    monkeypatch.setattr(service.sandboxes, "ensure_flow_run_runtime", ensure)
    with pytest.raises(DomainError) as raised:
        service._ensure_attempt_runtime_for_native_observation(db, attempt)
    assert raised.value.code == (
        "RUNTIME_SESSION_NOT_ACTIVE" if missing == "session" else "RUN_ENVIRONMENT_VERSION_INVALID"
    )
    ensure.assert_not_called()
