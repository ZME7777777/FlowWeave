"""Native child stream authorization, revocation and branch recovery over both hosts."""

from __future__ import annotations

import asyncio
from threading import Event

import pytest
import test_child_sessions as child_fixtures
from starlette.websockets import WebSocketDisconnect

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeEvent, RuntimeEventBatch, RuntimeInputReadiness
from flowweave.shared.errors import DomainError

parent_graph = child_fixtures.parent_graph
parent_profile = child_fixtures.parent_profile
gateway_base_url = child_fixtures.gateway_base_url
launcher_http = child_fixtures.launcher_http
child_http = child_fixtures.child_http
pytestmark = pytest.mark.parametrize("parent_profile", ["OH_FW_V4", "OH_FW_V5"], indirect=True)


@pytest.fixture
def native_stream(child_http, monkeypatch):
    x = child_http
    closed = Event()
    began = Event()
    release = Event()
    calls = []

    async def events(handle, *, after_seq=None):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        calls.append((handle.conversation_id, after_seq))
        began.set()
        try:
            yield {"type": "delta", "content": "ephemeral text must not be forwarded"}
            yield {"type": "durable_cursor", "seq": 7}
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield {"type": "stream_closed", "item_id": "original-native-message"}
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(x.runtime, "stream_events", events, raising=False)
    yield x, began, closed, release, calls
    release.set()


def test_original_child_stream_resumes_native_sequence_and_closes_idle_upstream(native_stream):
    x, began, closed, _, calls = native_stream
    with x.client.websocket_connect(x.root + "/stream?after_seq=6") as socket:
        assert socket.receive_json() == {"type": "durable_cursor", "seq": 7}
        assert began.is_set()
        assert calls == [(x.loc.handle.conversation_id, 6)]
    assert closed.wait(2)


@pytest.mark.parametrize("revocation", ["parent-delete", "child-delete", "session-fence"])
def test_open_child_stream_revalidates_before_forwarding_late_frames(native_stream, revocation):
    x, _, closed, release, _ = native_stream
    with x.client.websocket_connect(x.root + "/stream") as socket:
        assert socket.receive_json()["seq"] == 7
        with x.g.sessions() as db:
            if revocation == "session-fence":
                from flowweave.modules.agent_workspaces.infrastructure.models import (
                    AgentWorkspaceRuntime,
                )
                from flowweave.shared.models import FlowRunRuntime

                cls = AgentWorkspaceRuntime if x.g.kind == "AGENT_WORKSPACE" else FlowRunRuntime
                db.get(cls, x.g.runtime_id).status = "RECONNECTING"
            else:
                binding_id = x.loc.parent_id if revocation == "parent-delete" else x.loc.binding_id
                db.get(AgentConversationBinding, binding_id).lifecycle = "DELETE_PENDING"
            db.commit()
        release.set()
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()
        assert ended.value.code == 4409
    assert closed.wait(2)


@pytest.mark.parametrize("authority", ["other-user", "wrong-parent", "anonymous"])
def test_child_stream_never_opens_for_wrong_authority(native_stream, authority):
    x, began, _, _, calls = native_stream
    path = x.root + "/stream"
    if authority == "anonymous":
        x.client.cookies.clear()
    elif authority == "other-user":
        x.client.cookies.set("flowweave_session", "other-cookie")
    else:
        from uuid import uuid4

        path = path.replace(x.loc.parent_id, str(uuid4()))
    with tenant_user(x.g.other if authority == "other-user" else x.g.owner):
        with pytest.raises(WebSocketDisconnect) as ended:
            with x.client.websocket_connect(path) as socket:
                socket.receive_json()
    assert ended.value.code == (4401 if authority == "anonymous" else 4409)
    assert not began.is_set() and calls == []


@pytest.mark.parametrize("anchor", ["valid", "detached", "reported-detached", "outage"])
def test_child_hydration_marks_incremental_only_for_a_native_ancestor(
    child_http, monkeypatch, anchor
):
    x = child_http
    calls = []

    def read(handle):
        assert x.g.sessions.kw["bind"].pool.checkedout() == 0
        calls.append(handle.cursor)
        if anchor == "outage":
            raise DomainError("RUNTIME_BUSINESS_READ_TIMEOUT", "timeout", 504)
        if anchor == "detached" and handle.cursor:
            raise DomainError("RUNTIME_EVENT_IDENTITY_MISMATCH", "detached", 409)
        return RuntimeEventBatch(
            events=(
                RuntimeEvent("actual-leaf", "MESSAGE", {"source": "agent", "content": "native"}),
            ),
            cursor="actual-leaf",
            cursor_anchor_found=anchor != "reported-detached",
            readiness=RuntimeInputReadiness(True, "idle"),
            context={},
        )

    monkeypatch.setattr(x.runtime, "read_active_events", read)
    response = x.client.get(x.root + "/hydration?cursor=old-leaf")
    if anchor == "outage":
        assert response.status_code == 504 and calls == ["old-leaf"]
        return
    assert response.status_code == 200, response.text
    assert response.json().get("incremental_from") == ("old-leaf" if anchor == "valid" else None)
    assert response.json()["events"]["events"][0]["id"] == "actual-leaf"
    assert calls == (["old-leaf", None] if anchor == "detached" else ["old-leaf"])


def test_idle_child_stream_rechecks_authority_without_waiting_for_a_native_frame(
    native_stream, monkeypatch
):
    from flowweave.shared import runtime_stream

    x, _, closed, _, _ = native_stream
    sleep = asyncio.sleep

    async def fast_validation(seconds):
        await sleep(0.01 if seconds == 15 else seconds)

    monkeypatch.setattr(runtime_stream.asyncio, "sleep", fast_validation)
    with x.client.websocket_connect(x.root + "/stream") as socket:
        assert socket.receive_json()["seq"] == 7
        with x.g.sessions() as db:
            db.get(AgentConversationBinding, x.loc.parent_id).lifecycle = "DELETE_PENDING"
            db.commit()
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()
        assert ended.value.code == 4409
    assert closed.wait(2)
