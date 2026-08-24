from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from flowweave.modules.conversations.application import locator
from flowweave.modules.conversations.infrastructure.models import FlowRunConversationBinding
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    EnvironmentVersion,
    FlowDefinition,
    FlowRun,
    FlowRunRuntime,
    FlowRunRuntimeAllocation,
    FlowRunRuntimeSecretReference,
    TerminalEnvironment,
)


def _runtime_context(db: Session) -> tuple[str, str]:
    environment = TerminalEnvironment(
        name=f"environment-{uuid4()}",
        description="",
        base_image="python:3.13",
        base_image_digest="sha256:" + "1" * 64,
    )
    db.add(environment)
    db.flush()
    version = EnvironmentVersion(
        environment_id=environment.id,
        version_no=1,
        state="READY",
        base_image_reference="python@sha256:" + "1" * 64,
        base_image_digest="sha256:" + "1" * 64,
        image_reference="flowweave/environment-test:v1",
        image_digest="sha256:" + "2" * 64,
        manifest_json={},
    )
    flow = FlowDefinition(
        name=f"flow-{uuid4()}",
        description="",
        default_entry_key=None,
        lark_root_folder_url="",
    )
    db.add_all((version, flow))
    db.flush()
    run = FlowRun(
        flow_definition_id=flow.id,
        run_no=1,
        name="runtime locator test",
        state="ACTIVE",
        environment_version_id=version.id,
    )
    secret = FlowRunRuntimeSecretReference(
        encrypted_secret_key=b"encrypted",
        secret_digest=uuid4().hex + uuid4().hex,
    )
    db.add_all((run, secret))
    db.flush()
    allocation = FlowRunRuntimeAllocation(
        flow_run_id=run.id,
        secret_reference_id=secret.id,
        relative_root=f".flow-run-runtimes/{run.id}",
    )
    db.add(allocation)
    db.flush()
    runtime = FlowRunRuntime(
        flow_run_id=run.id,
        environment_version_id=version.id,
        runtime_image_digest=version.image_digest,
        workspace_allocation_id=allocation.id,
        status="STARTING",
    )
    db.add(runtime)
    db.flush()
    return run.id, runtime.id


def _connection(runtime_session_id: str, flow_run_id: str, *, generation: int = 1):
    return SimpleNamespace(
        runtime_session_id=runtime_session_id,
        flow_run_id=flow_run_id,
        managed_runtime_id=f"runtime-{generation}",
        resource_name=f"fw-sbx-generation-{generation}",
        generation=generation,
    )


def test_binding_is_an_idempotent_minimal_locator(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: _connection(runtime_session_id, flow_run_id),
        )

        first = locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            display_label="会话一",
        )
        second = locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            display_label="会话一（更新）",
        )

        assert second.id == first.id
        assert second.runtime_session_id == runtime_session_id
        assert second.openhands_conversation_id == "conversation-original"
        assert second.display_label == "会话一（更新）"
        assert set(FlowRunConversationBinding.__table__.columns.keys()) == {
            "id",
            "flow_run_id",
            "runtime_session_id",
            "openhands_conversation_id",
            "display_label",
            "created_at",
            "last_connected_at",
        }


def test_unbound_conversation_fails_closed(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        flow_run_id, _runtime_session_id = _runtime_context(db)

        with pytest.raises(DomainError) as caught:
            locator.conversation_locator(
                db,
                flow_run_id=flow_run_id,
                openhands_conversation_id="not-bound",
            )

        assert caught.value.code == "RUNTIME_CONVERSATION_UNBOUND"


def test_route_re_resolves_the_current_generation_without_changing_identity(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        current = _connection(runtime_session_id, flow_run_id, generation=1)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: current,
        )
        locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
        )

        current = _connection(runtime_session_id, flow_run_id, generation=2)
        handle = locator.active_runtime_handle(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
            cursor=None,
            route_kind="COLLABORATION",
        )

        assert handle.conversation_id == "conversation-original"
        assert handle.runtime_resource_id == "runtime-2"
        assert handle.runtime_resource_name == "fw-sbx-generation-2"
        assert handle.job_id == "env-chat:fw-sbx-generation-2"


def test_route_rejects_locator_session_drift(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_session_factory() as db:
        flow_run_id, runtime_session_id = _runtime_context(db)
        current = _connection(runtime_session_id, flow_run_id)
        monkeypatch.setattr(
            locator.sandboxes,
            "active_flow_run_runtime_connection",
            lambda _db, *, flow_run_id: current,
        )
        locator.bind_openhands_conversation(
            db,
            flow_run_id=flow_run_id,
            openhands_conversation_id="conversation-original",
        )

        current = _connection(str(uuid4()), flow_run_id, generation=2)
        with pytest.raises(DomainError) as caught:
            locator.active_runtime_handle(
                db,
                flow_run_id=flow_run_id,
                openhands_conversation_id="conversation-original",
                cursor=None,
                route_kind="EXECUTION",
            )

        assert caught.value.code == "RUNTIME_CONVERSATION_SESSION_DRIFT"
