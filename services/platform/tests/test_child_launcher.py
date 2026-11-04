"""Two-host native parent authority with real PostgreSQL binding records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from flowweave.modules.agent_sessions.application import child_launcher, child_parent
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import RuntimeAgentSpec, RuntimeTool
from flowweave.runtime.child_contract import (
    CHILD_TOOL_NAMES,
    CHILD_USAGE_GUIDANCE,
    V3_ARCHIVE_SHA256,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
)
from flowweave.runtime.child_creation import NativeChildInvocation, NativeChildLaunchRequest
from flowweave.runtime.child_governance import NativeChildResolution
from flowweave.runtime.child_governance_contract import (
    V4_ARCHIVE_SHA256,
    V4_SOURCE_COMMIT,
    governed_child_capability_manifest,
)
from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.runtime.protocol_profiles import runtime_contract_for_protocol_profile
from flowweave.shared.domain.openhands import OpenHandsServerIdentity
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRun, FlowRunRuntime, NodeAttempt, NodeRun, RunSnapshot, User
from flowweave.shared.settings import settings_context

FROZEN_V3_IDENTITY = OpenHandsServerIdentity("1.58.2", V3_SOURCE_COMMIT, V3_SOURCE_COMMIT)


@pytest.fixture
def gateway_base_url(request):
    if getattr(request, "param", None) == "local":
        import socket

        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{bound.getsockname()[1]}"
    return "https://gateway.example.test/prefix"


@pytest.fixture
def parent_profile(request):
    return getattr(request, "param", "OH_FW_V3")


@pytest.fixture(params=["AGENT_WORKSPACE", "FLOW_NODE"])
def parent_graph(
    request, db_session_factory, settings, monkeypatch, gateway_base_url, parent_profile
):
    identity = (
        FROZEN_V3_IDENTITY
        if parent_profile == "OH_FW_V3"
        else OpenHandsServerIdentity("1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT)
    )
    config = settings.model_copy(
        update={
            "runtime_adapter": "openhands",
            "agent_child_gateway_base_url": gateway_base_url,
        }
    )
    owner, other, workspace_id, runtime_id, host_id, scope_id, native_id, binding_id = [
        str(uuid4()) for _ in range(8)
    ]
    resource = "fr719-test-resource"
    image = "sha256:" + "a" * 64
    kind = request.param
    if kind == "AGENT_WORKSPACE":
        host_id = workspace_id
        scope_id = workspace_id
    manifest = {
        "schema_version": 3,
        "child_conversations": child_capability_manifest()
        if parent_profile == "OH_FW_V3"
        else governed_child_capability_manifest(),
        "nodes": {},
    }
    spec = RuntimeAgentSpec(
        tools=(RuntimeTool("task_tracker"),),
        runtime_contract=runtime_contract_for_protocol_profile(
            parent_profile, ("task_tracker",), identity
        ),
    )
    with settings_context(config), tenant_user(owner), db_session_factory() as db:
        db.add_all(
            [
                User(id=owner, username=owner, role="SUPER_ADMIN"),
                User(id=other, username=other, role="SUPER_ADMIN"),
            ]
        )
        if kind == "AGENT_WORKSPACE":
            db.add(AgentWorkspace(id=workspace_id, scope_key=workspace_id))
            db.add(
                AgentWorkspaceRuntime(
                    id=runtime_id,
                    workspace_id=workspace_id,
                    runtime_image_digest=image,
                    workspace_allocation_id=str(uuid4()),
                    active_generation=1,
                    status="ACTIVE",
                )
            )
            db.add(
                ManagedSandbox(
                    id=str(uuid4()),
                    kind="AGENT_RUNTIME",
                    owner_type="AGENT_WORKSPACE",
                    owner_id=workspace_id,
                    backend_resource_name=resource,
                    generation=1,
                    image_reference=image,
                    hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        else:
            run = FlowRun(
                id=host_id,
                flow_definition_id=str(uuid4()),
                run_no=1,
                name="native parent",
                state="RUNNING",
            )
            node = NodeRun(
                id=str(uuid4()), flow_run_id=host_id, flow_node_snapshot_key="node", sequence_no=1
            )
            snapshot = RunSnapshot(
                id=str(uuid4()),
                flow_run_id=host_id,
                version=1,
                definition_json={},
                definition_hash="a" * 64,
                runtime_manifest_json=manifest,
                runtime_manifest_hash=runtime_manifest_hash(manifest),
            )
            attempt = NodeAttempt(
                id=scope_id,
                node_run_id=node.id,
                snapshot_id=snapshot.id,
                attempt_no=1,
                state="RUNNING",
            )
            session = FlowRunRuntime(
                id=runtime_id,
                flow_run_id=host_id,
                runtime_image_digest=image,
                environment_version_id=str(uuid4()),
                workspace_allocation_id=str(uuid4()),
                protocol_profile=parent_profile,
                active_generation=1,
                status="ACTIVE",
            )
            db.add_all([run, node, snapshot, attempt, session])
            monkeypatch.setattr(
                child_launcher.sandboxes,
                "active_node_runtime_connection",
                lambda *_args, **_kwargs: SimpleNamespace(
                    runtime_session_id=runtime_id,
                    protocol_profile=SimpleNamespace(value=parent_profile),
                    generation=1,
                    resource_name=resource,
                    managed_runtime_id="node-runtime",
                ),
            )
        binding = AgentConversationBinding(
            id=binding_id,
            owner_user_id=owner,
            workspace_id=workspace_id if kind == "AGENT_WORKSPACE" else None,
            host_kind=kind,
            host_id=host_id,
            flow_run_id=host_id if kind == "FLOW_NODE" else None,
            conversation_scope_id=scope_id,
            runtime_session_id=runtime_id,
            openhands_conversation_id=native_id,
            working_directory=(
                f"/runtime/workspace/project/users/{owner}/test"
                if kind == "AGENT_WORKSPACE"
                else f"/runtime/workspace/project/{host_id}"
            ),
            create_idempotency_key=str(uuid4()),
            lifecycle="PROVISIONING",
        )
        db.add(binding)
        db.flush()
        bound_spec = child_parent.attach_parent_capability(
            db,
            binding,
            spec,
            profile=parent_profile,
            identity=identity,
            generation=1,
            runtime_image_digest=image,
            runtime_manifest=manifest,
        )
        frozen = child_parent.read_parent_capability(binding)
        binding.lifecycle = "ACTIVE"
        db.commit()
        key = SecretStr(
            derive_runtime_session_key(
                config.openhands_session_api_key, config.sandbox_manager_scope, resource
            )
        )
        yield SimpleNamespace(
            db=db,
            profile=parent_profile,
            identity=identity,
            owner=owner,
            other=other,
            binding=binding,
            frozen=frozen,
            spec=spec,
            bound_spec=bound_spec,
            key=key,
            config=config,
            manifest=manifest,
            runtime_id=runtime_id,
            kind=kind,
            image=image,
            sessions=db_session_factory,
        )


def prepare(g, **changes):
    kwargs = dict(owner_id=UUID(g.owner), binding_id=UUID(g.binding.id), runtime_key=g.key)
    kwargs.update(changes)
    return child_launcher.prepare_launcher(g.db, **kwargs)


def proof(g, **changes):
    value = dict(
        schema_version=1,
        operation="start",
        policy=g.frozen.policy,
        action_id="native-action",
        tool_call_id="native-tool-call",
        audience=g.frozen.launch_url,
        request_digest="a" * 64,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    value.update(changes)
    return NativeChildInvocation(**value)


def test_freeze_and_two_phase_auth_do_not_create_a_child(parent_graph):
    g = parent_graph
    names = [tool.name for tool in g.bound_spec.tools]
    assert tuple(names[-6:]) == CHILD_TOOL_NAMES
    assert "start_child_conversation" not in names
    assert CHILD_USAGE_GUIDANCE in g.bound_spec.agent_context.system_message_suffix
    assert str(g.frozen.policy.scope_id) == g.binding.id
    assert "/prefix/api/v1/agent-child-launchers/" in g.frozen.launch_url
    prepared = prepare(g)
    assert child_launcher.project_launcher(g.db, prepared, proof(g), runtime_key=g.key) == prepared
    assert not g.db.scalar(select(AgentChildConversation.id))
    assert len(list(g.db.scalars(select(AgentConversationBinding.id)))) == 1


@pytest.mark.parametrize("change", ["user", "binding", "key", "root-key"])
def test_wrong_user_binding_or_runtime_credential_cannot_authorize(parent_graph, change):
    g = parent_graph
    kwargs = {
        "user": {"owner_id": UUID(g.other)},
        "binding": {"binding_id": uuid4()},
        "key": {"runtime_key": SecretStr("forged")},
        "root-key": {"runtime_key": SecretStr(g.config.openhands_session_api_key)},
    }[change]
    with pytest.raises(DomainError) as caught:
        prepare(g, **kwargs)
    assert caught.value.code == "AGENT_CHILD_LAUNCH_DENIED"


@pytest.mark.parametrize(
    "field",
    [
        "generation",
        "scope_id",
        "runtime_session_id",
        "parent_conversation_id",
        "manifest_digest",
        "max_active_children",
        "audience",
    ],
)
def test_native_claim_must_match_every_frozen_authority_field(parent_graph, field):
    g = parent_graph
    prepared = prepare(g)
    if field == "audience":
        invocation = proof(g, audience="https://untrusted.example.test")
    else:
        replacement = {"generation": 2, "manifest_digest": "b" * 64, "max_active_children": 1}.get(
            field, uuid4()
        )
        invocation = proof(g, policy=g.frozen.policy.model_copy(update={field: replacement}))
    with pytest.raises(DomainError):
        child_launcher.project_launcher(g.db, prepared, invocation, runtime_key=g.key)


@pytest.mark.parametrize(
    "change", ["disabled", "permission", "generation", "image", "runtime", "deleting"]
)
def test_revocation_or_runtime_drift_after_native_verification_is_rejected(parent_graph, change):
    g = parent_graph
    prepared = prepare(g)
    session_type = AgentWorkspaceRuntime if g.kind == "AGENT_WORKSPACE" else FlowRunRuntime
    if change in {"disabled", "permission"}:
        user = g.db.get(User, g.owner)
        if change == "disabled":
            user.is_active = False
        else:
            user.role = "USER"
    elif change == "deleting":
        g.binding.lifecycle = "DELETE_PENDING"
    else:
        runtime = g.db.get(session_type, g.runtime_id)
        if change == "generation":
            runtime.active_generation = 2
        elif change == "image":
            runtime.runtime_image_digest = "sha256:" + "b" * 64
        else:
            runtime.status = "RECONNECTING"
    g.db.commit()
    with pytest.raises(DomainError):
        child_launcher.project_launcher(g.db, prepared, proof(g), runtime_key=g.key)


@pytest.mark.parametrize(
    "field",
    [
        "owner_user_id",
        "host_id",
        "conversation_scope_id",
        "runtime_session_id",
        "openhands_conversation_id",
        "working_directory",
        "child_parent_manifest_json",
    ],
)
def test_database_freezes_parent_identity_and_capability(parent_graph, field):
    g = parent_graph
    value = "{}" if field == "child_parent_manifest_json" else str(uuid4())
    expression = "CAST(:value AS JSON)" if field == "child_parent_manifest_json" else ":value"
    with pytest.raises(DBAPIError, match="immutable"):
        g.db.execute(
            text(f"UPDATE agent_conversation_bindings SET {field} = {expression} WHERE id = :id"),
            {"value": value, "id": g.binding.id},
        )
        g.db.commit()
    g.db.rollback()


def test_frozen_parent_still_allows_title_and_unread(parent_graph):
    g = parent_graph
    g.binding.display_title = "new title"
    g.binding.unread = True
    g.db.commit()
    assert prepare(g).capability == g.frozen


def test_legacy_profiles_do_not_install_parent_tools(parent_graph):
    g = parent_graph
    old = AgentConversationBinding(child_parent_manifest_json=None)
    for profile in ("OH_FW_V1", "OH_FW_V2"):
        assert (
            child_parent.attach_parent_capability(
                g.db,
                old,
                g.spec,
                profile=profile,
                identity=FROZEN_V3_IDENTITY,
                generation=1,
                runtime_image_digest=g.image,
                runtime_manifest={},
            )
            is g.spec
        )
    assert old.child_parent_manifest_json is None


@pytest.mark.parametrize(
    "failure", ["unknown-source", "wrong-manifest", "new-generation", "wrong-snapshot-digest"]
)
def test_bad_freeze_inputs_fail_before_tool_installation(parent_graph, failure):
    g = parent_graph
    kwargs = dict(
        profile="OH_FW_V3",
        identity=FROZEN_V3_IDENTITY,
        generation=1,
        runtime_image_digest=g.image,
        runtime_manifest=g.manifest,
    )
    if failure == "unknown-source":
        kwargs["identity"] = replace(FROZEN_V3_IDENTITY, source_commit="b" * 40)
    elif failure == "wrong-manifest":
        kwargs["runtime_manifest"] = {"child_conversations": {}}
    elif failure == "new-generation":
        kwargs["generation"] = 2
    else:
        kwargs["expected_manifest_digest"] = "b" * 64
    with pytest.raises(DomainError):
        child_parent.attach_parent_capability(g.db, g.binding, g.spec, **kwargs)


def test_native_verifier_is_required_and_receives_opaque_proof(parent_graph, monkeypatch):
    g = parent_graph
    prepared = prepare(g)
    calls = []

    def verify(handle, request):
        calls.append((handle, request))
        raise DomainError("RUNTIME_CHILD_REQUEST_REJECTED", "native denied", 403)

    monkeypatch.setattr(
        child_launcher, "get_runtime", lambda: SimpleNamespace(verify_child_launcher=verify)
    )
    request = NativeChildLaunchRequest(task="task", token=SecretStr("opaque"))
    with pytest.raises(DomainError):
        child_launcher.verify_launcher(prepared, request)
    assert calls == [(prepared.handle, request)]


@pytest.fixture
def launcher_http(parent_graph, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, Mock

    from fastapi.testclient import TestClient

    from flowweave.bootstrap import api as bootstrap

    g = parent_graph
    calls = []

    def verify(handle, request):
        calls.append((handle, request))
        if request.token.get_secret_value() != "native-proof":
            raise child_parent.denied()
        return proof(g, request_digest=request.request_digest())

    def resolve(handle, request):
        return resolution(verify(handle, request))

    class AsyncSession:
        def __init__(self, db):
            self.db = db

        async def run_sync(self, operation):
            return operation(self.db)

        async def commit(self):
            self.db.commit()

        async def rollback(self):
            self.db.rollback()

    @asynccontextmanager
    async def session_scope():
        with g.sessions() as db:
            yield AsyncSession(db)

    with ThreadPoolExecutor(max_workers=1) as executor:
        container = SimpleNamespace(
            settings=g.config,
            database=SimpleNamespace(
                session=session_scope,
                blocking_sessions=g.sessions,
                history_sessions=g.sessions,
            ),
            blocking_executor=executor,
            history_read_executor=executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_io_slots=asyncio.Semaphore(1),
            blocking_capacity=1,
            metrics=Mock(),
            rate_limiter=SimpleNamespace(
                allow=AsyncMock(return_value=SimpleNamespace(allowed=True))
            ),
            runtime=fake_creation_runtime(g, verify, resolve),
            artifact_store=Mock(),
            plugin_resolver=Mock(),
            sandbox=Mock(),
            audit_writer=Mock(),
            conversation_hydration_cache=Mock(),
        )
        monkeypatch.setattr(bootstrap, "build_container", lambda *_args, **_kwargs: container)
        app = bootstrap.create_app(g.config)
        client = TestClient(app)
        try:
            yield g, client, calls
        finally:
            client.close()


@pytest.mark.parametrize(
    "credential", ["absent", "cookie-only", "root-key", "wrong-key", "runtime-only", "valid"]
)
@pytest.mark.parametrize("parent_profile", ["OH_FW_V3", "OH_FW_V4"], indirect=True)
def test_http_gateway_requires_runtime_key_and_native_proof(launcher_http, credential):
    from urllib.parse import urlsplit

    g, client, calls = launcher_http
    path = urlsplit(g.frozen.launch_url).path.removeprefix("/prefix")
    headers = {}
    if credential in {"root-key", "wrong-key", "runtime-only", "valid"}:
        headers["X-Session-API-Key"] = {
            "root-key": g.config.openhands_session_api_key,
            "wrong-key": "forged",
        }.get(credential, g.key.get_secret_value())
    if credential == "cookie-only":
        client.cookies.set("flowweave_session", "browser-cookie")
    response = client.post(
        path,
        headers=headers,
        json={
            "task": "task",
            "title": None,
            "isolation": "shared",
            "token": "native-proof" if credential == "valid" else "forged-proof",
        },
    )
    expected = (200 if g.profile == "OH_FW_V4" else 503) if credential == "valid" else 403
    assert response.status_code == expected, response.text
    if credential == "valid" and g.profile == "OH_FW_V3":
        assert response.json()["error"]["code"] == "AGENT_CHILD_CREATION_NOT_ENABLED"
    if credential == "valid":
        audit = client.app.state.container.audit_writer.submit.call_args.args[0]
        assert audit.user_id == g.owner
    assert len(calls) == (1 if credential in {"runtime-only", "valid"} else 0)
    assert bool(g.db.scalar(select(AgentChildConversation.id))) == (expected == 200)


@pytest.mark.parametrize(
    "body", [b'{"token":"sensitive-proof","task":12}', b"invalid", b"x" * 262145]
)
def test_http_invalid_body_never_echoes_proof_or_creates_children(launcher_http, body):
    from urllib.parse import urlsplit

    g, client, calls = launcher_http
    path = urlsplit(g.frozen.launch_url).path.removeprefix("/prefix")
    response = client.post(
        path, headers={"X-Session-API-Key": g.key.get_secret_value()}, content=body
    )
    assert response.status_code == (413 if len(body) > 262144 else 422)
    assert "sensitive-proof" not in response.text
    assert not calls


def test_only_exact_native_path_bypasses_cookie_login(launcher_http):
    from urllib.parse import urlsplit

    g, client, _ = launcher_http
    path = urlsplit(g.frozen.launch_url).path.removeprefix("/prefix")
    for extra in ("/queries/anything", "/commands/anything", "/start/anything"):
        response = client.post(path.removesuffix("/start") + extra, json={})
        assert response.status_code == 401


@pytest.mark.parametrize("gateway_base_url", ["local"], indirect=True)
@pytest.mark.parametrize("parent_profile", ["OH_FW_V3", "OH_FW_V4"], indirect=True)
def test_native_tool_calls_platform_gateway_over_real_tcp(launcher_http, monkeypatch):
    """Real native action -> platform authorization -> native child lifecycle.

    V3 retains refusal; V4 creates exactly one child and one first user event.
    Only LLM completion, physical mount mapping and Runtime connection are fixtures.
    """
    import asyncio
    import json
    import os
    import subprocess
    import tempfile
    import threading
    import time
    from pathlib import Path
    from urllib.parse import urlsplit

    import httpx
    import uvicorn

    from flowweave.runtime.base import RuntimeProvider, StartAttemptRequest
    from flowweave.runtime.openhands import OpenHandsRuntime

    g, client, _ = launcher_http
    environment = (
        "FLOWWEAVE_NATIVE_CHILD_V3_PYTHON"
        if g.profile == "OH_FW_V3"
        else "FLOWWEAVE_NATIVE_CHILD_PYTHON"
    )
    python = os.environ.get(environment)
    if not python:
        pytest.skip(f"Set {environment} to the corresponding fixed native environment")
    repo = Path(__file__).resolve().parents[3]
    root = repo / ".tmp/fr723/native"
    root.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(dir=root))
    state.chmod(0o700)
    for directory in ("home", "tmp", "cache"):
        (state / directory).mkdir()
    config = state / "fixture.json"
    config.write_text(json.dumps({"runtime_key": g.key.get_secret_value()}))
    config.chmod(0o600)
    env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
    env.update(
        HOME=str(state / "home"),
        TMPDIR=str(state / "tmp"),
        XDG_CACHE_HOME=str(state / "cache"),
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    source_lock = json.loads((repo / "infra/openhands/source.lock.json").read_text())
    source_lock.update(
        source_commit=g.identity.source_commit,
        fork_commit=g.identity.source_commit,
        archive_sha256=V3_ARCHIVE_SHA256 if g.profile == "OH_FW_V3" else V4_ARCHIVE_SHA256,
    )
    source_lock_path = state / "source.lock.json"
    source_lock_path.write_text(json.dumps(source_lock))
    env["FLOWWEAVE_NATIVE_CHILD_LOCK"] = str(source_lock_path)
    if g.profile == "OH_FW_V4":
        env["FLOWWEAVE_CHILD_INTERACTIONS"] = "1"
    script = Path(__file__).parents[1] / "unit_tests/fixtures/native_child_server.py"
    native = OpenHandsRuntime(g.config)
    authorized_calls = []
    original_project = child_launcher.project_launcher

    def record_authorization(db, prepared, verified, *, runtime_key):
        result = original_project(db, prepared, verified, runtime_key=runtime_key)
        claim = verified.invocation if isinstance(verified, NativeChildResolution) else verified
        with tenant_user(g.owner):
            saved = db.get(AgentConversationBinding, str(prepared.capability.binding_id))
            authorized_calls.append((result.capability.profile, saved.lifecycle, claim.action_id))
        return result

    monkeypatch.setattr(child_launcher, "project_launcher", record_authorization)
    server = None
    thread = None
    with (state / "native.log").open("w") as log:
        process = subprocess.Popen(
            [str(Path(python).absolute()), str(script), str(state), str(repo), "serve"],
            env=env,
            cwd=state,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while not (state / "server-ready.json").exists():
                assert process.poll() is None, (
                    f"Native fixture failed; inspect {state / 'native.log'}"
                )
                assert time.monotonic() < deadline
                time.sleep(0.05)
            info = json.loads((state / "server-ready.json").read_text())
            assert info["verified_python_files"] == (536 if g.profile == "OH_FW_V3" else 537)
            with httpx.Client(base_url=info["host"], trust_env=False, timeout=10) as http:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if http.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            original_workspace = native._request_workspace_path

            def mounted_workspace(request):
                assert original_workspace(request) == request.workspace_ref
                return str(state / "workspace")

            # Simulate the container mount without writing /runtime on the host.
            monkeypatch.setattr(native, "_request_workspace_path", mounted_workspace)
            monkeypatch.setattr(native, "_base_url_for_handle", lambda _: info["host"])
            monkeypatch.setattr(
                native, "_session_key_for_handle", lambda _: g.key.get_secret_value()
            )
            if g.profile == "OH_FW_V4":
                original_request = native._request

                def mounted_response(method, path, **kwargs):
                    # Simulate container mount paths on actual HTTP responses;
                    # keep the production state/config identity checks enabled.
                    value = original_request(method, path, **kwargs)
                    parts = path.strip("/").split("/")
                    if (
                        method == "GET"
                        and len(parts) == 3
                        and parts[:2] == ["api", "conversations"]
                    ):
                        workspace = value.get("workspace")
                        if workspace is not None:
                            assert workspace["working_dir"] == str(state / "workspace")
                            value["workspace"] = {
                                **workspace,
                                "working_dir": g.binding.working_directory,
                            }
                            value["persistence_dir"] = (
                                f"/runtime/state/conversations/{UUID(parts[2]).hex}"
                            )
                    if "/children/preparations/" in path and value.get("workspace") is not None:
                        assert value["workspace"] == str(state / "workspace")
                        value["workspace"] = g.binding.working_directory
                    return value

                monkeypatch.setattr(native, "_request", mounted_response)
            client.app.state.container.runtime = native
            gateway = urlsplit(g.config.agent_child_gateway_base_url)
            server = uvicorn.Server(
                uvicorn.Config(
                    client.app,
                    host="127.0.0.1",
                    port=gateway.port,
                    lifespan="off",
                    log_level="error",
                )
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            spec = replace(
                g.bound_spec,
                provider=RuntimeProvider(
                    "fixture", "https://model.example.test", "openai/test", "unused"
                ),
            )
            request = StartAttemptRequest(
                attempt_id=g.binding.id,
                execution_key="agent-workspace:native-parent",
                node={},
                bindings=[],
                workspace_ref=f"/runtime/workspace/project/{g.binding.host_id}",
                workspace_root=f"/runtime/workspace/project/{g.binding.host_id}",
                conversation_id=g.binding.openhands_conversation_id,
                agent_spec=spec,
                environment_image=g.image,
                environment_id=str(uuid4()),
                environment_version_id=str(uuid4()),
                environment_version_no=1,
                runtime_sandbox_id="native-fixture",
                runtime_resource_name="fr719-test-resource",
                runtime_base_url=info["host"],
                conversation_secrets={
                    "FLOWWEAVE_HISTORY_ACCESS": "fixture-parent-only",
                    "ORDINARY": "fixture-ordinary",
                }
                if g.profile == "OH_FW_V4"
                else {},
            )
            if g.profile == "OH_FW_V4":
                # Reproduce the first-turn window: parent binding not ACTIVE yet,
                # but frozen authority was committed before native execution.
                g.binding.lifecycle = "PROVISIONING"
                g.db.commit()
            handle = native.create_conversation(request)
            assert handle.protocol_profile == g.profile
            if g.profile == "OH_FW_V4":
                native.update_conversation_secrets(
                    handle,
                    {
                        "LATE_ONLY": "fixture-additive",
                    },
                )
            native.send_message(handle, "delegate")
            with httpx.Client(
                base_url=info["host"],
                headers={"X-Session-API-Key": g.key.get_secret_value()},
                timeout=10,
                trust_env=False,
            ) as http:
                conversation_root = f"/api/conversations/{handle.conversation_id}"
                deadline = time.monotonic() + 15
                while http.get(conversation_root).json()["execution_status"] != "finished":
                    assert time.monotonic() < deadline, "Native parent did not finish"
                    time.sleep(0.05)
                events = http.get(conversation_root + "/events/search").json()["items"]
                actions = [
                    e
                    for e in events
                    if e["kind"] == "ActionEvent" and e.get("tool_name") == CHILD_TOOL_NAMES[0]
                ]
                assert len(actions) == 1
                observations = [
                    e
                    for e in events
                    if e["kind"] == "ObservationEvent" and e.get("action_id") == actions[0]["id"]
                ]
                assert len(observations) == 1
                assert observations[0]["observation"]["is_error"] == (g.profile == "OH_FW_V3")
                assert observations[0]["observation"]["outcome_unknown"] == (
                    g.profile == "OH_FW_V3"
                )
                if g.profile == "OH_FW_V4":
                    all_actions = [
                        e
                        for e in events
                        if e["kind"] == "ActionEvent" and e.get("tool_name") in CHILD_TOOL_NAMES
                    ]
                    assert len(all_actions) == 6
                    all_observations = [
                        e
                        for e in events
                        if e["kind"] == "ObservationEvent"
                        and e.get("action_id") in {a["id"] for a in all_actions}
                    ]
                    assert len(all_observations) == 6
                    assert not any(e["observation"].get("is_error") for e in all_observations), (
                        all_observations
                    )
                assert authorized_calls
                assert all(
                    call
                    == (
                        g.profile,
                        "PROVISIONING" if g.profile == "OH_FW_V4" else "ACTIVE",
                        actions[0]["id"],
                    )
                    for call in authorized_calls
                )
            signed = json.loads((state / "proof.json").read_text())
            invocation = NativeChildInvocation.model_validate(signed["invocation"])
            assert invocation.audience == g.frozen.launch_url
            assert invocation.action_id == actions[0]["id"]
            # Replaying the genuine proof returns the same native child (V3 remains gated).
            with httpx.Client(trust_env=False, timeout=10) as http:
                accepted = http.post(
                    g.frozen.launch_url,
                    headers={"X-Session-API-Key": g.key.get_secret_value()},
                    json={
                        "task": "review child 🌳",
                        "title": "child",
                        "isolation": "shared",
                        "token": signed["token"],
                    },
                )
                assert accepted.status_code == (200 if g.profile == "OH_FW_V4" else 503), (
                    accepted.text
                )
                if g.profile == "OH_FW_V3":
                    assert accepted.json()["error"]["code"] == "AGENT_CHILD_CREATION_NOT_ENABLED"
                else:
                    child_id = accepted.json()["conversation_id"]
                    child_root = f"/api/conversations/{child_id}"
                    with httpx.Client(
                        base_url=info["host"],
                        headers={"X-Session-API-Key": g.key.get_secret_value()},
                        trust_env=False,
                    ) as child_http:
                        deadline = time.monotonic() + 10
                        while child_http.get(child_root).json()["execution_status"] != "finished":
                            assert time.monotonic() < deadline
                            time.sleep(0.05)
                        child_events = child_http.get(child_root + "/events/search").json()["items"]
                        users = [
                            e
                            for e in child_events
                            if e["kind"] == "MessageEvent" and e.get("source") == "user"
                        ]
                        assert len(users) == 2
                        parent_inputs = [
                            e
                            for e in users
                            if (e.get("parent_instruction") or {}).get("operation") == "send"
                        ]
                        assert len(parent_inputs) == 1
                        assert (
                            parent_inputs[0]["parent_instruction"]["parent_conversation_id"]
                            == handle.conversation_id
                        )
                        assert "review child" in str(users[0])
                        configuration = child_http.get(child_root).json()
                        names = configuration["secret_registry"]["secret_sources"]
                        assert "FLOWWEAVE_HISTORY_ACCESS" not in names
                        assert "ORDINARY" in names
                        assert "LATE_ONLY" not in names
                        assert not set(CHILD_TOOL_NAMES) & {
                            t["name"] for t in configuration["agent"]["tools"]
                        }
                        assert configuration["parent_conversation_id"] == handle.conversation_id
            if g.profile == "OH_FW_V4":
                from flowweave.bootstrap import api as bootstrap_api
                from flowweave.modules.users.application.security import Principal

                monkeypatch.setattr(
                    bootstrap_api.users,
                    "authenticate",
                    lambda db, cookie: Principal(g.owner, g.owner, "SUPER_ADMIN", True),
                )
                client.cookies.set("flowweave_session", "fixture-cookie")
                browser_root = (
                    f"/api/v1/agent-session-children/{g.binding.id}/{accepted.json()['binding_id']}"
                )
                # The first-turn parent window ends after its native tools.
                g.binding.lifecycle = "ACTIVE"
                g.db.commit()
                submission = str(uuid4())
                user_message = {"submission_id": submission, "content": "user follow-up"}
                user_sent = client.post(browser_root + "/messages", json=user_message)
                assert user_sent.status_code == 202, user_sent.text
                replay = client.post(browser_root + "/messages", json=user_message)
                assert (
                    replay.status_code == 202
                    and replay.json()["cursor"] == user_sent.json()["cursor"]
                ), replay.text
                hydration = client.get(browser_root + "/hydration")
                assert hydration.status_code == 200, hydration.text
                assert client.get(browser_root + "/context").status_code == 200
                # A real server shutdown/restart must preserve native gate,
                # initial event and the platform reservation with the same proof.
                process.stdin.write("stop\n")
                process.stdin.close()
                process.wait(timeout=15)
                assert process.returncode == 0
                (state / "server-ready.json").unlink()
                process = subprocess.Popen(
                    [str(Path(python).absolute()), str(script), str(state), str(repo), "serve"],
                    env=env,
                    cwd=state,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=log,
                    text=True,
                )
                deadline = time.monotonic() + 30
                while not (state / "server-ready.json").exists():
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                info = json.loads((state / "server-ready.json").read_text())
                with httpx.Client(
                    base_url=info["host"],
                    headers={"X-Session-API-Key": g.key.get_secret_value()},
                    trust_env=False,
                    timeout=10,
                ) as reloaded:
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            if reloaded.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
                    with httpx.Client(trust_env=False, timeout=10) as gateway_http:
                        restored = gateway_http.post(
                            g.frozen.launch_url,
                            headers={"X-Session-API-Key": g.key.get_secret_value()},
                            json={
                                "task": "review child 🌳",
                                "title": "child",
                                "isolation": "shared",
                                "token": signed["token"],
                            },
                        )
                    assert restored.status_code == 200, restored.text
                    assert restored.json() == accepted.json()
                    saved_events = reloaded.get(child_root + "/events/search").json()["items"]
                    assert (
                        len(
                            [
                                e
                                for e in saved_events
                                if e["kind"] == "MessageEvent" and e.get("source") == "user"
                            ]
                        )
                        == 3
                    )
                    names = reloaded.get(child_root).json()["secret_registry"]["secret_sources"]
                    assert "FLOWWEAVE_HISTORY_ACCESS" not in names
                    assert "ORDINARY" in names
            assert bool(g.db.scalar(select(AgentChildConversation.id))) == (g.profile == "OH_FW_V4")
        finally:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=15)
                assert not thread.is_alive()
            asyncio.run(native.aclose())
            if process.stdin:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            assert process.returncode == 0, f"Native fixture failed; inspect {state / 'native.log'}"


def test_old_active_binding_cannot_acquire_parent_capability(parent_graph):
    g = parent_graph
    other = AgentConversationBinding(
        id=str(uuid4()),
        workspace_id=g.binding.workspace_id,
        owner_user_id=g.owner,
        host_kind=g.kind,
        host_id=g.binding.host_id,
        conversation_scope_id=g.binding.conversation_scope_id,
        runtime_session_id=g.runtime_id,
        openhands_conversation_id=str(uuid4()),
        flow_run_id=g.binding.flow_run_id,
        working_directory=g.binding.working_directory,
        create_idempotency_key=str(uuid4()),
        lifecycle="ACTIVE",
    )
    g.db.add(other)
    g.db.commit()
    with pytest.raises(DomainError):
        child_parent.attach_parent_capability(
            g.db,
            other,
            g.spec,
            profile="OH_FW_V3",
            identity=FROZEN_V3_IDENTITY,
            generation=1,
            runtime_image_digest=g.image,
            runtime_manifest=g.manifest,
        )
    with pytest.raises(DBAPIError, match="existing conversation"):
        g.db.execute(
            text(
                "UPDATE agent_conversation_bindings "
                "SET child_parent_manifest_json = CAST(:value AS JSON) WHERE id = :id"
            ),
            {"value": "{}", "id": other.id},
        )
    g.db.rollback()


def test_downgrade_refuses_to_discard_parent_capability(parent_graph):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    g = parent_graph
    original_head = g.db.scalar(text("SELECT version_num FROM alembic_version"))
    g.db.rollback()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = g.config.database_url
    with pytest.raises(DBAPIError, match="Cannot discard frozen"):
        command.downgrade(config, "0176_agent_child_conversations")
    assert (
        g.db.scalar(text("SELECT version_num FROM alembic_version"))
        == original_head
    )
    assert child_parent.read_parent_capability(g.binding) == g.frozen


def test_fork_cannot_copy_parent_bound_authority(parent_graph):
    with pytest.raises(DomainError) as caught:
        child_parent.require_forkable_parent(parent_graph.binding)
    assert caught.value.code == "AGENT_CHILD_PARENT_FORK_UNSUPPORTED"
    child_parent.require_forkable_parent(AgentConversationBinding(child_parent_manifest_json=None))


@pytest.mark.parametrize("parent_graph", ["AGENT_WORKSPACE"], indirect=True)
@pytest.mark.parametrize("allow_children", [True, False])
@pytest.mark.parametrize("parent_profile", ["OH_FW_V3", "OH_FW_V4"], indirect=True)
def test_workspace_native_creation_attaches_only_explicit_parent_capability(
    parent_graph, monkeypatch, tmp_path, allow_children
):
    from flowweave.modules.agent_sessions.application import conversations
    from flowweave.modules.agent_sessions.application.runtime_config import FrozenSessionConfig
    from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeProvider
    from flowweave.runtime.dependencies import runtime_context

    g = parent_graph
    binding = AgentConversationBinding(
        id=str(uuid4()),
        owner_user_id=g.owner,
        workspace_id=g.binding.workspace_id,
        host_kind=g.kind,
        host_id=g.binding.host_id,
        conversation_scope_id=g.binding.conversation_scope_id,
        runtime_session_id=g.runtime_id,
        openhands_conversation_id=str(uuid4()),
        working_directory=g.binding.working_directory,
        create_idempotency_key=str(uuid4()),
        lifecycle="PROVISIONING",
    )
    g.db.add(binding)
    g.db.flush()
    monkeypatch.setattr(conversations, "apply_profile_identity", lambda *_args: None)
    materialized_root = g.config.workspace_root / "fr719-capabilities" / str(uuid4())
    monkeypatch.setattr(
        conversations,
        "_capability_marketplace_paths",
        lambda *_args: (
            materialized_root,
            Path("/runtime/capabilities/fr719"),
            "test",
        ),
    )
    monkeypatch.setattr(
        conversations,
        "config_from_binding",
        lambda *_args: FrozenSessionConfig(None, None, None, None, ()),
    )
    captured = []

    def create(request):
        if g.profile == "OH_FW_V4" and allow_children:
            assert not g.db.in_transaction()
            with tenant_user(g.owner), g.sessions() as reader:
                saved = reader.get(AgentConversationBinding, binding.id)
                assert saved.child_parent_manifest_json["profile"] == "OH_FW_V4"
        captured.append(request)
        return conversations._handle(g.db, g.db.get(AgentWorkspace, binding.workspace_id), binding)

    def reload(handle):
        return RuntimeConversationIdentity(
            handle.conversation_id,
            binding.working_directory,
            f"/runtime/state/conversations/{UUID(binding.openhands_conversation_id).hex}",
            None,
            None,
            None,
            None,
        )

    with (
        settings_context(
            g.config.model_copy(update={"agent_workspace_runtime_protocol_profile": g.profile})
        ),
        runtime_context(SimpleNamespace(create_conversation=create, reload_conversation=reload)),
    ):
        handle = conversations._create_native_conversation(
            g.db,
            g.db.get(AgentWorkspace, binding.workspace_id),
            binding,
            RuntimeProvider("fixture", "https://model.example.test", "test", "unused"),
            binding.working_directory,
            credential_ids=(),
            allow_child_capability=allow_children,
        )
    assert len(captured) == 1
    names = {tool.name for tool in captured[0].agent_spec.tools}
    assert (set(CHILD_TOOL_NAMES) <= names) == allow_children
    assert (binding.child_parent_manifest_json is not None) == allow_children
    assert handle.protocol_profile == (g.profile if allow_children else None)


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
def test_cancellation_and_snapshot_drift_deny_flow_node_parent(parent_graph):
    g = parent_graph
    prepared = prepare(g)
    attempt = g.db.get(NodeAttempt, g.binding.conversation_scope_id)
    attempt.state = "CANCELLED"
    g.db.commit()
    with pytest.raises(DomainError):
        child_launcher.project_launcher(g.db, prepared, proof(g), runtime_key=g.key)
    attempt.state = "RUNNING"
    snapshot = g.db.get(RunSnapshot, attempt.snapshot_id)
    snapshot.runtime_manifest_hash = "b" * 64
    g.db.commit()
    with pytest.raises(DomainError):
        prepare(g)


def test_parent_entry_uses_independent_database_phases(launcher_http, monkeypatch):
    from urllib.parse import urlsplit

    g, client, _ = launcher_http
    observed = []

    def verify(handle, request):
        # The fixture itself has no checked-out transaction at this point.
        assert g.sessions.kw["bind"].pool.checkedout() == 0
        observed.append(True)
        return proof(g, request_digest=request.request_digest())

    client.app.state.container.runtime.verify_child_launcher = verify
    response = client.post(
        urlsplit(g.frozen.launch_url).path.removeprefix("/prefix"),
        headers={"X-Session-API-Key": g.key.get_secret_value()},
        json={"task": "task", "token": "native-proof"},
    )
    assert response.status_code == 503, response.text
    assert observed == [True]


@pytest.mark.parametrize("parent_graph", ["AGENT_WORKSPACE"], indirect=True)
def test_empty_manifest_migration_roundtrip_preserves_legacy_binding(parent_graph):
    from alembic import command
    from alembic.config import Config

    g = parent_graph
    legacy_id = str(uuid4())
    g.db.add(
        AgentConversationBinding(
            id=legacy_id,
            owner_user_id=g.owner,
            workspace_id=g.binding.workspace_id,
            host_kind=g.kind,
            host_id=g.binding.host_id,
            conversation_scope_id=g.binding.conversation_scope_id,
            runtime_session_id=g.runtime_id,
            openhands_conversation_id=str(uuid4()),
            working_directory=g.binding.working_directory,
            create_idempotency_key=str(uuid4()),
            lifecycle="ACTIVE",
        )
    )
    g.db.delete(g.binding)
    g.db.commit()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = g.config.database_url
    try:
        command.downgrade(config, "0176_agent_child_conversations")
    finally:
        command.upgrade(config, "head")
    legacy = g.db.get(AgentConversationBinding, legacy_id)
    assert legacy is not None and legacy.child_parent_manifest_json is None
    assert legacy.lifecycle == "ACTIVE"


@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize(
    "parent_profile,leased",
    [("OH_FW_V3", True), ("OH_FW_V4", True), ("OH_FW_V4", False)],
    indirect=["parent_profile"],
)
def test_worker_freezes_parent_before_releasing_database_transaction(
    parent_graph, monkeypatch, leased
):
    from flowweave.modules.orchestration.application import service
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.dependencies import runtime_context

    g = parent_graph
    attempt = g.db.get(NodeAttempt, g.binding.conversation_scope_id)
    attempt.state, attempt.runtime_phase = "EXECUTING", "STARTING"
    parent = AgentConversationBinding(
        id=str(uuid4()),
        owner_user_id=g.owner,
        host_kind=g.kind,
        host_id=g.binding.host_id,
        flow_run_id=g.binding.host_id,
        conversation_scope_id=attempt.id,
        runtime_session_id=g.runtime_id,
        openhands_conversation_id=str(uuid4()),
        working_directory=g.binding.working_directory,
        node_attempt_id=attempt.id,
        node_run_id=attempt.node_run_id,
        create_idempotency_key=str(uuid4()),
        lifecycle="PROVISIONING",
    )
    g.db.add(parent)
    g.db.commit()
    parent_id, attempt_id = parent.id, attempt.id
    request = StartAttemptRequest(
        attempt_id=attempt.id,
        execution_key="fixture",
        node={},
        bindings=[],
        workspace_ref=parent.working_directory,
        agent_spec=g.spec,
        conversation_id=parent.openhands_conversation_id,
        environment_image=g.image,
        environment_id=str(uuid4()),
        environment_version_id=str(uuid4()),
        environment_version_no=1,
    )
    environment = SimpleNamespace(
        id=request.environment_version_id,
        image_digest=g.image,
        manifest_json={},
        environment_id=request.environment_id,
        version_no=1,
        runtime_protocol_profile=g.profile,
    )
    monkeypatch.setattr(service, "_runtime_request", lambda *_: request)
    monkeypatch.setattr(service, "lock_referenceable_version", lambda *_: environment)
    monkeypatch.setattr(
        service,
        "runtime_server_identity",
        lambda *_args, **_kwargs: g.identity,
    )
    monkeypatch.setattr(
        service.sandboxes,
        "node_attempt_workspace_context",
        lambda *_args, **_kwargs: SimpleNamespace(attempt_owned=False),
    )
    monkeypatch.setattr(
        service.sandboxes,
        "ensure_flow_run_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="node-runtime",
            resource_name="fr719-test-resource",
            base_url="http://runtime.example.test",
        ),
    )
    monkeypatch.setattr(
        service.agent_sessions,
        "flow_node_binding_for_attempt",
        lambda db, *_args, **_kwargs: db.get(AgentConversationBinding, parent_id),
    )
    lease_checks = []
    monkeypatch.setattr(service, "_require_current_lease", lambda *_args: lease_checks.append(True))
    captured = []

    def start(compiled):
        assert not g.db.in_transaction()
        with tenant_user(g.owner), g.sessions() as reader:
            saved = reader.get(AgentConversationBinding, parent_id)
            assert saved.child_parent_manifest_json is not None
        captured.append(compiled)
        raise DomainError("FIXTURE_STOP", "Stop before native execution", 409)

    with runtime_context(SimpleNamespace(start=start)), pytest.raises(DomainError) as stopped:
        service.process_start_runtime(g.db, attempt_id, lease=SimpleNamespace() if leased else None)
    assert stopped.value.code == "FIXTURE_STOP"
    assert lease_checks == ([True, True] if g.profile == "OH_FW_V4" else [True])
    assert set(CHILD_TOOL_NAMES) <= {tool.name for tool in captured[0].agent_spec.tools}


def resolution(invocation):
    from uuid import uuid5

    from flowweave.runtime.child_creation import NativeChildIdentity

    parent = invocation.policy.parent_conversation_id
    creation = uuid5(
        parent, f"openhands-child-invocation-v1:{invocation.action_id}:{invocation.tool_call_id}"
    )
    return NativeChildResolution(
        schema_version=1,
        invocation=invocation,
        identity=NativeChildIdentity(
            parent_conversation_id=parent,
            creation_id=creation,
            conversation_id=uuid5(parent, f"openhands-child-creation-v1:{creation.hex}"),
        ),
    )


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("lifecycle", ["ACTIVE", "PROVISIONING"])
def test_v4_native_resolution_authorizes_both_first_turn_and_active_parent(
    parent_graph, monkeypatch, lifecycle
):
    g = parent_graph
    g.binding.lifecycle = lifecycle
    g.db.commit()
    prepared = prepare(g)
    request = NativeChildLaunchRequest(task="task", token=SecretStr("native-proof"))
    verified = resolution(proof(g, request_digest=request.request_digest()))
    calls = []

    def resolve(handle, launch):
        calls.append((handle, launch))
        return verified

    monkeypatch.setattr(
        child_launcher, "get_runtime", lambda: SimpleNamespace(resolve_child_launcher=resolve)
    )
    assert child_launcher.verify_launcher(prepared, request) == verified
    assert calls == [(prepared.handle, request)]
    assert child_launcher.project_launcher(g.db, prepared, verified, runtime_key=g.key) == prepared
    with pytest.raises(DomainError):
        child_launcher.project_launcher(g.db, prepared, verified.invocation, runtime_key=g.key)


@pytest.mark.parametrize("parent_profile", ["OH_FW_V3", "OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("lifecycle", ["PROVISIONING", "FAILED", "DELETE_PENDING"])
def test_parent_lifecycle_candidates_are_version_scoped(parent_graph, lifecycle):
    g = parent_graph
    g.binding.lifecycle = lifecycle
    g.db.commit()
    if g.profile == "OH_FW_V4" and lifecycle == "PROVISIONING":
        assert prepare(g).capability.profile == "OH_FW_V4"
    else:
        with pytest.raises(DomainError):
            prepare(g)


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("change", ["generation", "deleting", "disabled", "permission", "image"])
def test_v4_provisioning_revocation_between_native_phases_is_denied(parent_graph, change):
    g = parent_graph
    g.binding.lifecycle = "PROVISIONING"
    g.db.commit()
    prepared = prepare(g)
    verified = resolution(proof(g))
    if change == "deleting":
        g.binding.lifecycle = "DELETE_PENDING"
    elif change in {"disabled", "permission"}:
        user = g.db.get(User, g.owner)
        if change == "disabled":
            user.is_active = False
        else:
            user.role = "USER"
    else:
        session_type = AgentWorkspaceRuntime if g.kind == "AGENT_WORKSPACE" else FlowRunRuntime
        runtime = g.db.get(session_type, g.runtime_id)
        if change == "generation":
            runtime.active_generation = 2
        else:
            runtime.runtime_image_digest = "sha256:" + "b" * 64
    g.db.commit()
    with pytest.raises(DomainError):
        child_launcher.project_launcher(g.db, prepared, verified, runtime_key=g.key)


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("parent_graph", ["FLOW_NODE"], indirect=True)
@pytest.mark.parametrize("entry", ["create", "bootstrap"])
def test_v4_manual_node_entries_commit_parent_before_native_create(
    parent_graph, monkeypatch, tmp_path, entry
):
    from flowweave.modules.agent_sessions.application import flow_node_conversations as node
    from flowweave.modules.agent_sessions.application.runtime_config import FrozenSessionConfig
    from flowweave.runtime.base import StartAttemptRequest
    from flowweave.runtime.dependencies import runtime_context

    g = parent_graph
    attempt = g.db.get(NodeAttempt, g.binding.conversation_scope_id)
    run = g.db.get(FlowRun, g.binding.host_id)
    snapshot = g.db.get(RunSnapshot, attempt.snapshot_id)
    run.environment_version_id = snapshot.environment_version_id = str(uuid4())
    parent = AgentConversationBinding(
        id=str(uuid4()),
        owner_user_id=g.owner,
        host_kind=g.kind,
        host_id=run.id,
        flow_run_id=run.id,
        conversation_scope_id=attempt.id,
        runtime_session_id=g.runtime_id,
        openhands_conversation_id=str(uuid4()),
        working_directory=g.binding.working_directory,
        node_attempt_id=attempt.id,
        node_run_id=attempt.node_run_id,
        create_idempotency_key=str(uuid4()),
        lifecycle="PROVISIONING",
    )
    g.db.add(parent)
    g.db.flush()
    environment = SimpleNamespace(
        id=run.environment_version_id,
        image_digest=g.image,
        manifest_json={},
        environment_id=str(uuid4()),
        version_no=1,
        runtime_protocol_profile=g.profile,
    )
    monkeypatch.setattr(node, "lock_referenceable_version", lambda *_: environment)
    monkeypatch.setattr(node, "runtime_server_identity", lambda *_args, **_kwargs: g.identity)
    monkeypatch.setattr(
        node, "_node_capability_root", lambda *_args, **_kwargs: tmp_path / parent.id
    )
    monkeypatch.setattr(node, "reserve_flow_node_binding", lambda *_args, **_kwargs: parent)
    monkeypatch.setattr(
        node, "config_from_binding", lambda *_: FrozenSessionConfig(None, None, None, None, ())
    )
    monkeypatch.setattr(node, "provider_for_config", lambda *_: None)
    monkeypatch.setattr(node, "_materialize_conversation_memory", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(node, "_node_context_suffix", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(node, "credentials_for_agent", lambda *_: ({}, ""))
    monkeypatch.setattr(node, "build_agent_spec", lambda *_args, **_kwargs: g.spec)
    monkeypatch.setattr(
        node.sandboxes,
        "node_attempt_workspace_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            runtime_working_directory=parent.working_directory
        ),
    )

    def build(_db, **kwargs):
        return StartAttemptRequest(
            attempt_id=parent.id,
            execution_key="manual-node",
            node={},
            bindings=[],
            workspace_ref=parent.working_directory,
            conversation_id=parent.openhands_conversation_id,
            agent_spec=kwargs["agent_spec"],
        )

    monkeypatch.setattr(node, "build_runtime_request", build)
    captured = []
    parent_id = parent.id

    def create(request):
        assert not g.db.in_transaction()
        with tenant_user(g.owner), g.sessions() as reader:
            saved = reader.get(AgentConversationBinding, parent_id)
            assert saved.lifecycle == "PROVISIONING"
            assert child_parent.read_parent_capability(saved).profile == "OH_FW_V4"
        captured.append(request)
        raise DomainError("FIXTURE_STOP", "Stop before native execution", 409)

    with (
        runtime_context(SimpleNamespace(create_conversation=create)),
        pytest.raises(DomainError) as stopped,
    ):
        if entry == "create":
            node._create_native_conversation(
                g.db,
                run=run,
                snapshot=snapshot,
                title=None,
                idempotency_key=str(uuid4()),
                binding_id=parent.id,
                attempt_id=attempt.id,
                node_run_id=attempt.node_run_id,
                session_binding_id=parent.id,
            )
        else:
            node._create_or_reload_node_bootstrap(
                g.db,
                binding=parent,
                run=run,
                snapshot=snapshot,
                allow_existing=False,
            )
    assert stopped.value.code == "FIXTURE_STOP"
    assert set(CHILD_TOOL_NAMES) <= {tool.name for tool in captured[0].agent_spec.tools}


@pytest.mark.parametrize("parent_profile", ["OH_FW_V3", "OH_FW_V4"], indirect=True)
def test_frozen_parent_cannot_change_protocol_or_capability_version(parent_graph):
    g = parent_graph
    target = "OH_FW_V4" if g.profile == "OH_FW_V3" else "OH_FW_V3"
    identity = (
        OpenHandsServerIdentity("1.58.2", V4_SOURCE_COMMIT, V4_SOURCE_COMMIT)
        if target == "OH_FW_V4"
        else FROZEN_V3_IDENTITY
    )
    manifest = {
        **g.manifest,
        "child_conversations": governed_child_capability_manifest()
        if target == "OH_FW_V4"
        else child_capability_manifest(),
    }
    spec = replace(
        g.spec,
        runtime_contract=runtime_contract_for_protocol_profile(target, ("task_tracker",), identity),
    )
    original = g.binding.child_parent_manifest_json
    with pytest.raises(DomainError):
        child_parent.attach_parent_capability(
            g.db,
            g.binding,
            spec,
            profile=target,
            identity=identity,
            generation=1,
            runtime_image_digest=g.image,
            runtime_manifest=manifest,
        )
    assert g.binding.child_parent_manifest_json == original
    assert child_parent.read_parent_capability(g.binding).profile == g.profile


def fake_creation_runtime(g, verify, resolve):
    """Stateful formal-port fixture; native lifecycle is proved separately over TCP."""
    from flowweave.modules.agent_sessions.application import child_creation
    from flowweave.runtime.child_creation import NativeChildConfiguration, NativeChildReceipt
    from flowweave.runtime.child_governance import (
        NativeGovernedChildPreparation,
        NativeParentConfiguration,
    )

    parent = NativeParentConfiguration(
        id=g.binding.openhands_conversation_id,
        workspace={"working_dir": g.binding.working_directory},
        persistence_dir=f"/state/{UUID(g.binding.openhands_conversation_id).hex}",
        agent={
            "kind": "Agent",
            "llm": {"model": "test"},
            "tools": [{"name": name} for name in (*CHILD_TOOL_NAMES, "task_tracker")],
            "mcp_config": {"flowweave-reference-history": {"url": "https://history.example.test"}},
            "agent_context": {
                "secrets": {
                    "FLOWWEAVE_HISTORY_ACCESS": {"kind": "StaticSecret", "value": "**********"}
                }
            },
        },
    )
    saved = {}
    configs = {}
    released = []

    def io():
        assert g.sessions.kw["bind"].pool.checkedout() == 0

    def parent_configuration(handle):
        io()
        return parent

    def prepare_child(handle, creation, request, overrides):
        io()
        identity = resolution(proof(g, request_digest=request.request_digest())).identity
        assert creation == identity.creation_id
        if creation not in saved:
            saved[creation] = NativeGovernedChildPreparation(
                receipt=NativeChildReceipt(
                    schema_version=1,
                    identity=identity,
                    request_digest=request.request_digest(),
                    created_at=datetime.now(UTC),
                ),
                phase="prepared",
                governance_id=overrides.governance_id,
                workspace=parent.workspace.working_dir,
            )
            configs[str(identity.conversation_id)] = NativeChildConfiguration(
                id=identity.conversation_id,
                parent_conversation_id=identity.parent_conversation_id,
                workspace=parent.workspace,
                persistence_dir=f"/state/{identity.conversation_id.hex}",
                agent=child_creation.expected_agent(parent),
            )
        assert saved[creation].governance_id == overrides.governance_id
        return saved[creation]

    def child_configuration(handle):
        io()
        return configs[handle.conversation_id]

    def release(handle, creation, request):
        io()
        if saved[creation].phase != "started":
            released.append(creation)
            saved[creation] = saved[creation].model_copy(update={"phase": "started"})
        return saved[creation]

    return SimpleNamespace(
        verify_child_launcher=verify,
        resolve_child_launcher=resolve,
        parent_conversation_configuration=parent_configuration,
        prepare_governed_child_conversation=prepare_child,
        child_conversation_configuration=child_configuration,
        release_child_conversation=release,
        saved=saved,
        configurations=configs,
        released=released,
    )


def post_child(g, client):
    from urllib.parse import urlsplit

    g.db.commit()
    return client.post(
        urlsplit(g.frozen.launch_url).path.removeprefix("/prefix"),
        headers={"X-Session-API-Key": g.key.get_secret_value()},
        json={"task": "private-child-task", "token": "native-proof"},
    )


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize(
    "boundary", ["prepare-before", "prepare-after", "config", "release-before", "release-after"]
)
def test_child_replay_recovers_original_reservation_without_double_release(
    launcher_http, monkeypatch, boundary
):
    g, client, _ = launcher_http
    runtime = client.app.state.container.runtime
    method = {
        "prepare-before": "prepare_governed_child_conversation",
        "prepare-after": "prepare_governed_child_conversation",
        "config": "child_conversation_configuration",
        "release-before": "release_child_conversation",
        "release-after": "release_child_conversation",
    }[boundary]
    original = getattr(runtime, method)

    def fail(*args):
        if boundary.endswith("after") or boundary == "config":
            original(*args)
        raise DomainError(
            "FIXTURE_ACK_LOST", "native response lost", 504, {"outcome_unknown": True}
        )

    monkeypatch.setattr(runtime, method, fail)
    first = post_child(g, client)
    assert first.status_code == 504, first.text
    assert first.json()["error"]["details"]["outcome_unknown"] is True
    g.db.expire_all()
    relation = g.db.scalar(select(AgentChildConversation))
    assert relation is not None
    binding_id, creation_id = relation.child_binding_id, relation.creation_id
    # Durable audit stores no original task, proof, secret value, or agent document.
    assert "private-child-task" not in repr(relation.__dict__)
    assert "native-proof" not in repr(relation.__dict__)
    g.db.commit()
    monkeypatch.setattr(runtime, method, original)
    retried = post_child(g, client)
    assert retried.status_code == 200, retried.text
    assert retried.json()["binding_id"] == binding_id
    again = post_child(g, client)
    assert again.json() == retried.json()
    assert runtime.released == [UUID(creation_id)]
    assert len(list(g.db.scalars(select(AgentChildConversation)))) == 1


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("mismatch", ["agent", "workspace", "persistence", "governance"])
def test_child_mismatched_preparation_never_releases(launcher_http, monkeypatch, mismatch):
    g, client, _ = launcher_http
    runtime = client.app.state.container.runtime
    method = (
        "prepare_governed_child_conversation"
        if mismatch == "governance"
        else "child_conversation_configuration"
    )
    original = getattr(runtime, method)

    def mismatched(*args):
        actual = original(*args)
        updates = {
            "agent": {"agent": {"kind": "Agent", "tools": []}},
            "workspace": {"workspace": {"working_dir": "/other"}},
            "persistence": {"persistence_dir": "/other"},
            "governance": {"governance_id": uuid4()},
        }[mismatch]
        return type(actual).model_validate({**actual.model_dump(), **updates})

    monkeypatch.setattr(runtime, method, mismatched)
    response = post_child(g, client)
    assert response.status_code == 409, response.text
    assert not runtime.released
    relation = g.db.scalar(select(AgentChildConversation))
    child = g.db.get(AgentConversationBinding, relation.child_binding_id)
    assert child.lifecycle == "PROVISIONING"


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("boundary", ["prepare", "release"])
def test_child_revocation_between_native_and_projection_preserves_reservation(
    launcher_http, monkeypatch, boundary
):
    g, client, _ = launcher_http
    runtime = client.app.state.container.runtime
    method = (
        "prepare_governed_child_conversation"
        if boundary == "prepare"
        else "release_child_conversation"
    )
    original = getattr(runtime, method)

    def revoke(*args):
        result = original(*args)
        with tenant_user(g.owner), g.sessions() as db:
            db.get(User, g.owner).is_active = False
            db.commit()
        return result

    monkeypatch.setattr(runtime, method, revoke)
    response = post_child(g, client)
    assert response.status_code == (403 if boundary == "prepare" else 504), response.text
    g.db.expire_all()
    relation = g.db.scalar(select(AgentChildConversation))
    assert relation is not None
    assert len(runtime.released) == (1 if boundary == "release" else 0)
    g.db.rollback()
    assert post_child(g, client).status_code == 403


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_child_native_capacity_and_expired_proof_keep_reservation(launcher_http, monkeypatch):
    g, client, _ = launcher_http
    runtime = client.app.state.container.runtime

    def saturated(*_):
        raise DomainError("RUNTIME_CHILD_REQUEST_REJECTED", "capacity", 429, {"status_code": 429})

    monkeypatch.setattr(runtime, "prepare_governed_child_conversation", saturated)
    assert post_child(g, client).status_code == 429
    relation = g.db.scalar(select(AgentChildConversation))
    assert relation is not None
    saved = relation.id
    g.db.rollback()

    def expired(*_):
        raise child_parent.denied()

    monkeypatch.setattr(runtime, "resolve_child_launcher", expired)
    assert post_child(g, client).status_code == 403
    assert g.db.get(AgentChildConversation, saved) is not None
    assert not runtime.released


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_concurrent_native_identity_reserves_one_child(parent_graph):
    from concurrent.futures import ThreadPoolExecutor

    from flowweave.modules.agent_sessions.application import child_creation

    g = parent_graph
    parent = prepare(g)
    invocation = resolution(proof(g))
    g.db.rollback()
    runtime = fake_creation_runtime(g, None, None)
    g.db.commit()
    config = runtime.parent_conversation_configuration(parent.handle)
    verified = child_creation.VerifiedCreation(invocation, config)

    def reserve(_):
        with settings_context(g.config), tenant_user(g.owner), g.sessions() as db:
            saved = child_creation.reserve_creation(db, parent, verified, runtime_key=g.key)
            db.commit()
            return saved.binding_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(reserve, range(4)))
    assert len(set(results)) == 1
    relation = g.db.scalar(select(AgentChildConversation))
    assert relation.child_binding_id == str(results[0])
    assert relation.request_digest == invocation.invocation.request_digest
    assert len(list(g.db.scalars(select(AgentConversationBinding)))) == 2


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_governed_creation_audit_is_immutable_and_cannot_be_downgraded(launcher_http):
    from alembic import command
    from alembic.config import Config

    g, client, _ = launcher_http
    created = post_child(g, client)
    assert created.status_code == 200, created.text
    relation = g.db.scalar(select(AgentChildConversation))
    assert relation.creation_policy_version == "binding-isolated-v1"
    relation_id = relation.id
    with pytest.raises(DBAPIError, match="immutable"):
        g.db.execute(
            text("UPDATE agent_child_conversations SET request_digest = :digest WHERE id = :id"),
            {"digest": "b" * 64, "id": relation_id},
        )
        g.db.commit()
    g.db.rollback()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = g.config.database_url
    with pytest.raises(DBAPIError, match="Cannot discard governed child creation audit"):
        command.downgrade(config, "0178_add_oh_fw_v4_protocol_profile")
    assert g.db.get(AgentChildConversation, relation_id).request_digest is not None


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_child_copies_capability_provenance_without_false_credential_sync(launcher_http):
    from flowweave.modules.agent_sessions.infrastructure.models import (
        AgentConversationCapability,
        AgentConversationCredentialSync,
    )

    g, client, _ = launcher_http
    for position, key in enumerate(("ordinary-skill", "flowweave-reference-history")):
        g.db.add(
            AgentConversationCapability(
                binding_id=g.binding.id,
                capability_version_id=str(uuid4()),
                capability_type="SKILL" if position == 0 else "MCP",
                capability_key=key,
                digest="b" * 64,
                position=position,
            )
        )
    g.db.add(
        AgentConversationCredentialSync(
            binding_id=g.binding.id,
            credential_id=str(uuid4()),
            credential_row_version=1,
            context_version=1,
        )
    )
    g.binding.credential_sync_initialized_at = datetime.now(UTC)
    g.db.commit()
    response = post_child(g, client)
    assert response.status_code == 200, response.text
    child_id = response.json()["binding_id"]
    refs = list(
        g.db.scalars(
            select(AgentConversationCapability).where(
                AgentConversationCapability.binding_id == child_id
            )
        )
    )
    assert [(ref.capability_key, ref.digest, ref.position) for ref in refs] == [
        ("ordinary-skill", "b" * 64, 0)
    ]
    assert not g.db.scalar(
        select(AgentConversationCredentialSync.id).where(
            AgentConversationCredentialSync.binding_id == child_id
        )
    )
    child = g.db.get(AgentConversationBinding, child_id)
    assert child.credential_sync_initialized_at is None
    assert child.node_attempt_id is None and child.node_run_id is None
    assert child.child_parent_manifest_json is None


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_child_refuses_acp_before_reserving_any_locator(launcher_http, monkeypatch):
    g, client, _ = launcher_http
    runtime = client.app.state.container.runtime
    original = runtime.parent_conversation_configuration
    monkeypatch.setattr(
        runtime,
        "parent_conversation_configuration",
        lambda handle: original(handle).model_copy(update={"agent": {"kind": "ACPAgent"}}),
    )
    response = post_child(g, client)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AGENT_CHILD_AGENT_UNSUPPORTED"
    assert not g.db.scalar(select(AgentChildConversation))
    assert not runtime.saved


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
def test_child_replay_cannot_change_intent_and_does_not_freeze_post_start_model(
    launcher_http, monkeypatch
):
    from urllib.parse import urlsplit

    g, client, _ = launcher_http
    first = post_child(g, client)
    assert first.status_code == 200, first.text
    runtime = client.app.state.container.runtime
    original = runtime.child_conversation_configuration
    monkeypatch.setattr(
        runtime,
        "child_conversation_configuration",
        lambda handle: original(handle).model_copy(
            update={"agent": {"kind": "Agent", "llm": {"model": "changed-by-user"}}}
        ),
    )
    assert post_child(g, client).json() == first.json()
    response = client.post(
        urlsplit(g.frozen.launch_url).path.removeprefix("/prefix"),
        headers={"X-Session-API-Key": g.key.get_secret_value()},
        json={"task": "changed", "token": "native-proof"},
    )
    assert response.status_code == 409, response.text
    assert len(runtime.released) == 1


@pytest.mark.parametrize("parent_profile", ["OH_FW_V4"], indirect=True)
@pytest.mark.parametrize("phase", ["resolve", "prepare", "release"])
def test_child_response_deadline_never_projects_late_native_result(
    launcher_http, monkeypatch, phase
):
    import asyncio
    import threading
    import time
    from urllib.parse import urlsplit

    import httpx

    g, client, _ = launcher_http
    container = client.app.state.container
    container.settings = g.config.model_copy(update={"runtime_event_read_timeout_seconds": 0.15})
    runtime = container.runtime
    method = {
        "resolve": "resolve_child_launcher",
        "prepare": "prepare_governed_child_conversation",
        "release": "release_child_conversation",
    }[phase]
    original = getattr(runtime, method)
    completed = threading.Event()

    def late(*args):
        result = original(*args)
        time.sleep(0.35)
        completed.set()
        return result

    monkeypatch.setattr(runtime, method, late)

    async def check():
        # Keep a real ASGI loop alive through the timed-out worker's callback;
        # a fresh TestClient portal per request would discard that callback.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
        ) as http:

            async def post():
                return await http.post(
                    urlsplit(g.frozen.launch_url).path.removeprefix("/prefix"),
                    headers={"X-Session-API-Key": g.key.get_secret_value()},
                    json={"task": "private-child-task", "token": "native-proof"},
                )

            response = await post()
            assert response.status_code == 504, response.text
            assert response.json()["error"]["details"]["outcome_unknown"] is True
            assert await asyncio.to_thread(completed.wait, 3)
            deadline = time.monotonic() + 3
            while container.blocking_io_slots.locked():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            relation = g.db.scalar(select(AgentChildConversation))
            if phase == "resolve":
                assert relation is None
            else:
                assert relation is not None
                child = g.db.get(AgentConversationBinding, relation.child_binding_id)
                assert child.lifecycle == ("ACTIVE" if phase == "release" else "PROVISIONING")
                assert child.last_message_at is None
            g.db.commit()
            monkeypatch.setattr(runtime, method, original)
            container.settings = g.config
            retry = await post()
            assert retry.status_code == 200, retry.text
            assert len(runtime.released) == 1

    asyncio.run(check())
