from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from flowweave.modules.agent_sessions.application import message_submissions as submissions
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentMessageSubmission,
)
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    Principal,
    bind_principal,
    reset_principal,
)
from flowweave.shared.errors import DomainError


@pytest.fixture
def submission_scope(db_session_factory):
    token = bind_principal(Principal(FLOWWEAVE_USER_ID, "flowweave", "USER", True))
    identity = str(uuid4())
    with db_session_factory() as db:
        db.add(
            AgentConversationBinding(
                id=identity,
                runtime_session_id=str(uuid4()),
                host_id=str(uuid4()),
                conversation_scope_id=str(uuid4()),
                openhands_conversation_id=str(uuid4()),
                create_idempotency_key=str(uuid4()),
                lifecycle="ACTIVE",
            )
        )
        db.commit()
    try:
        yield identity, db_session_factory
    finally:
        reset_principal(token)


def test_cancel_before_send_reaches_worker_is_a_durable_tombstone(submission_scope):
    binding, sessions = submission_scope
    with sessions() as db:
        assert submissions.cancel(db, binding, "early") == {"cancelled": True}
        db.commit()
    with sessions() as db, pytest.raises(DomainError, match="已取消") as raised:
        submissions.begin(db, binding, "early")
    assert raised.value.code == "AGENT_MESSAGE_CANCELLED"


def test_cancel_preparation_prevents_any_native_send(submission_scope):
    binding, sessions = submission_scope
    with sessions() as db:
        submissions.begin(db, binding, "preparing")
        db.commit()
    with sessions() as other_worker:
        assert submissions.cancel(other_worker, binding, "preparing")["cancelled"]
        other_worker.commit()
    calls = []
    runtime = SimpleNamespace(send_message=lambda *_: calls.append("send"))
    fence = submissions.DispatchFence(sessions, binding, "preparing")
    with submissions.fenced(fence), pytest.raises(DomainError) as raised:
        submissions.dispatch(runtime, object(), "must not send")
    assert raised.value.code == "AGENT_MESSAGE_CANCELLED"
    assert calls == []


def test_cancel_during_native_dispatch_interrupts_after_append(submission_scope):
    binding, sessions = submission_scope
    with sessions() as db:
        submissions.begin(db, binding, "native")
        db.commit()
    entered, release = Event(), Event()
    calls = []

    def send(*_args):
        entered.set()
        assert release.wait(5)
        calls.append("append")
        return "formal-result"

    runtime = SimpleNamespace(send_message=send, interrupt=lambda *_: calls.append("interrupt"))
    fence = submissions.DispatchFence(sessions, binding, "native")

    def dispatch():
        with submissions.fenced(fence):
            return submissions.dispatch(runtime, object(), "message")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(copy_context().run, dispatch)
        try:
            assert entered.wait(5)
            with sessions() as other_worker:
                assert submissions.cancel(other_worker, binding, "native") == {"cancelled": False}
                other_worker.commit()
        finally:
            release.set()
        assert future.result(timeout=5) == "formal-result"
    assert calls == ["append", "interrupt"]
    # No retry with the same request identity may append a second event.
    with sessions() as db, pytest.raises(DomainError) as raised:
        submissions.begin(db, binding, "native")
    assert raised.value.code == "AGENT_MESSAGE_SUBMISSION_EXISTS"


def test_cancellation_tenant_and_binding_boundaries_and_cascade(submission_scope):
    binding, sessions = submission_scope
    with sessions() as db:
        submissions.begin(db, binding, "scope")
        db.commit()
    other_token = bind_principal(Principal(str(uuid4()), "other", "USER", True))
    try:
        with sessions() as db:
            assert db.scalar(select(AgentMessageSubmission)) is None
        with pytest.raises(DomainError):
            submissions.DispatchFence(sessions, binding, "scope").claim()
    finally:
        reset_principal(other_token)
    with sessions() as db:
        assert db.scalar(select(AgentMessageSubmission)).phase == "PREPARING"
        # Native DB RLS, not just the ORM filter, is installed and forced.
        policy = db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname='agent_message_submissions'"
            )
        ).one()
        assert tuple(policy) == (True, True)
        db.execute(delete(AgentConversationBinding).where(AgentConversationBinding.id == binding))
        db.commit()
    with sessions() as db:
        assert db.scalar(select(AgentMessageSubmission)) is None


def test_cancel_http_authorizes_host_and_blocks_delayed_post_without_runtime(
    client,
    settings,
    submission_scope,
    monkeypatch,
):
    from flowweave.modules.agent_sessions.application import conversations
    from flowweave.modules.agent_workspaces.application.service import (
        ensure_default_agent_workspace,
    )

    binding, sessions = submission_scope
    from flowweave.shared.settings import settings_context

    with settings_context(settings), sessions() as db:
        workspace = ensure_default_agent_workspace(db)
        item = db.get(AgentConversationBinding, binding)
        item.workspace_id = workspace.id
        item.host_id = workspace.id
        workspace_id = workspace.id
        db.commit()
    monkeypatch.setattr(
        conversations, "get_runtime", lambda: pytest.fail("Cancellation must not read Runtime")
    )
    base = f"/api/v1/agent-workspaces/{workspace_id}/conversations/{binding}"
    response = client.post(base + "/messages/http-request/cancel", json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"cancelled": True}
    response = client.post(
        base + "/messages",
        json={"content": "never delivered"},
        headers={"Idempotency-Key": "http-request"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AGENT_MESSAGE_CANCELLED"
    wrong = base.replace(workspace_id, str(uuid4()))
    assert client.post(wrong + "/messages/other/cancel", json={}).status_code == 404


def test_failed_preparation_can_retry_but_dispatch_or_cancel_cannot(submission_scope):
    binding, sessions = submission_scope
    with sessions() as db:
        submissions.begin(db, binding, "retry")
        db.commit()
    with sessions() as db:
        submissions.discard_preparation(db, binding, "retry")
        db.commit()
    with sessions() as db:
        submissions.begin(db, binding, "retry")
        db.commit()
    submissions.DispatchFence(sessions, binding, "retry").claim()
    with sessions() as db:
        submissions.discard_preparation(db, binding, "retry")
        assert db.scalar(select(AgentMessageSubmission)).phase == "DISPATCHING"
        db.commit()


def test_http_cancel_wins_during_real_message_worker_preparation(
    client,
    settings,
    db_session_factory,
    monkeypatch,
):
    from test_agent_workspaces import _ready_workspace_for_conversation

    from flowweave.modules.agent_sessions.application import conversations
    from flowweave.runtime.dependencies import runtime_context
    from flowweave.shared.settings import settings_context

    runtime = client.app.state.container.runtime
    with settings_context(settings), db_session_factory() as db, runtime_context(runtime):
        workspace = _ready_workspace_for_conversation(db)
        created = conversations.create_conversation(
            db, workspace.id, None, workspace.default_model_provider_id, "worker-cancel-create"
        )
        workspace_id, binding_id = workspace.id, created["id"]
        db.commit()
    entered, release = Event(), Event()
    original = conversations.dispatch_running_message
    sent = []
    original_send = runtime.send_message

    def blocked(prepared):
        entered.set()
        assert release.wait(10)
        return original(prepared)

    def send(*args, **kwargs):
        sent.append(True)
        return original_send(*args, **kwargs)

    monkeypatch.setattr(conversations, "dispatch_running_message", blocked)
    monkeypatch.setattr(runtime, "send_message", send)
    base = f"/api/v1/agent-workspaces/{workspace_id}/conversations/{binding_id}/messages"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            base,
            json={"content": "cancel at preparation"},
            headers={"Idempotency-Key": "worker-cancel"},
        )
        try:
            assert entered.wait(10)
            response = client.post(base + "/worker-cancel/cancel", json={})
            assert response.status_code == 200, response.text
            assert response.json() == {"cancelled": True}
        finally:
            release.set()
        response = future.result(timeout=15)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AGENT_MESSAGE_CANCELLED"
    assert sent == []


def test_new_cancellation_routes_match_their_published_contract():
    import json
    from pathlib import Path

    from flowweave.bootstrap.api import create_app

    actual = create_app().openapi()["paths"]
    published = json.loads((Path(__file__).parents[3] / "contracts/openapi-v1.json").read_text())[
        "paths"
    ]
    paths = [path for path in actual if path.endswith("/messages/{submission_id}/cancel")]
    assert len(paths) == 2
    for path in paths:
        assert published[path] == actual[path]


def test_cancellation_migration_roundtrip_preserves_existing_bindings(
    submission_scope,
    settings,
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    binding, sessions = submission_scope
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.downgrade(config, "0184_cascade_directory_archives")
    with sessions() as db:
        assert "agent_message_submissions" not in inspect(db.get_bind()).get_table_names()
        assert db.get(AgentConversationBinding, binding) is not None
    command.upgrade(config, "head")
    with sessions() as db:
        assert db.get(AgentConversationBinding, binding) is not None
        assert (
            db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0185_message_submission_cancel"
        )
