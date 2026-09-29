from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from flowweave.bootstrap import api as api_module
from flowweave.bootstrap.container import build_container
from flowweave.bootstrap.settings import Settings
from flowweave.runtime.read_budget import hydration_read_budget
from flowweave.shared.application.transactions import (
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.http import (
    run_blocking,
    run_blocking_auxiliary,
    run_blocking_control,
    run_blocking_history,
    run_blocking_hydration,
    run_blocking_lifecycle,
    run_blocking_message,
    run_blocking_mutation,
)
from flowweave.shared.infrastructure.database import Database


@pytest.fixture(autouse=True)
def database():
    """These executor tests use in-memory session doubles, not PostgreSQL."""

    yield


@pytest.mark.asyncio
async def test_hydration_pool_partitions_existing_blocking_database_budget() -> None:
    resources = Database(Settings(blocking_pool_size=4, pool_max_overflow=0), hydration_pool_size=2)
    try:
        assert resources.blocking_engine.pool.size() == 2
        assert resources.hydration_engine is not None
        assert resources.hydration_engine.pool.size() == 2
        assert (
            resources.pool_metrics()["blocking"]["size"]
            + resources.pool_metrics()["hydration"]["size"]
            == 4
        )
    finally:
        await resources.dispose()


def test_message_pool_partitions_existing_blocking_database_budget() -> None:
    resources = Database(
        Settings(blocking_pool_size=4, pool_max_overflow=0),
        hydration_pool_size=2,
        message_pool_size=1,
    )
    try:
        assert resources.blocking_engine.pool.size() == 1
        assert resources.hydration_engine is not None
        assert resources.hydration_engine.pool.size() == 2
        assert resources.message_engine is not None
        assert resources.message_engine.pool.size() == 1
        metrics = resources.pool_metrics()
        assert (
            metrics["blocking"]["size"] + metrics["hydration"]["size"] + metrics["message"]["size"]
            == 4
        )
    finally:
        asyncio.run(resources.dispose())


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _Database:
    @contextmanager
    def blocking_sessions(self):
        yield _Session()

    @contextmanager
    def control_sessions(self):
        yield _Session()

    @contextmanager
    def history_sessions(self):
        yield _Session()

    @contextmanager
    def hydration_sessions(self):
        yield _Session()

    @contextmanager
    def lifecycle_sessions(self):
        yield _Session()

    @contextmanager
    def message_sessions(self):
        yield _Session()


@pytest.mark.asyncio
async def test_hydration_keeps_reserved_capacity_when_ordinary_read_is_stalled() -> None:
    started = threading.Event()
    release = threading.Event()

    def ordinary(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "ordinary"

    with (
        ThreadPoolExecutor(max_workers=1) as ordinary_executor,
        ThreadPoolExecutor(max_workers=1) as hydration_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=ordinary_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            hydration_executor=hydration_executor,
            hydration_io_slots=asyncio.Semaphore(1),
            hydration_capacity=1,
            blocking_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_size=2, blocking_pool_timeout_seconds=0.05),
        )
        blocked = asyncio.create_task(run_blocking(container, ordinary))
        assert await asyncio.to_thread(started.wait, 1)
        try:
            with hydration_read_budget(0.5):
                assert await run_blocking_hydration(container, lambda _session: "first-screen") == (
                    "first-screen"
                )
        finally:
            release.set()
        assert await blocked == "ordinary"


@pytest.mark.asyncio
async def test_cancelled_hydration_retains_slot_until_worker_exits() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "finished"

    with ThreadPoolExecutor(max_workers=1) as executor:
        container = SimpleNamespace(
            hydration_executor=executor,
            hydration_io_slots=asyncio.Semaphore(1),
            hydration_capacity=1,
            blocking_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_timeout_seconds=0.05),
        )
        with hydration_read_budget(0.5):
            request = asyncio.create_task(run_blocking_hydration(container, slow))
            assert await asyncio.to_thread(started.wait, 1)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            with pytest.raises(DomainError) as caught:
                await run_blocking_hydration(container, lambda _session: "incorrect")
            assert caught.value.code == "RUNTIME_READ_SATURATED"
            release.set()
            for _ in range(100):
                if not container.hydration_io_slots.locked():
                    break
                await asyncio.sleep(0.001)
            assert await run_blocking_hydration(container, lambda _session: "ready") == "ready"


@pytest.mark.asyncio
async def test_message_delivery_keeps_capacity_when_mutation_is_stalled() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_mutation(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "mutation"

    with (
        ThreadPoolExecutor(max_workers=1) as mutation_executor,
        ThreadPoolExecutor(max_workers=1) as message_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=mutation_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            blocking_mutation_slots=asyncio.Semaphore(1),
            blocking_capacity=1,
            message_executor=message_executor,
            message_io_slots=asyncio.Semaphore(1),
            message_capacity=1,
            database=_Database(),
            settings=SimpleNamespace(blocking_pool_size=2, blocking_pool_timeout_seconds=0.05),
        )
        blocked = asyncio.create_task(run_blocking_mutation(container, slow_mutation))
        assert await asyncio.to_thread(started.wait, 1)
        try:
            assert await run_blocking_message(container, lambda _session: "message") == "message"
        finally:
            release.set()
        assert await blocked == "mutation"


@pytest.mark.asyncio
async def test_run_blocking_keeps_cancelled_thread_counted_until_it_exits() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "finished"

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                blocking_pool_timeout_seconds=0.05,
                history_read_pool_size=1,
            ),
        )
        request = asyncio.create_task(run_blocking(container, blocked))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()

        # The synchronous operation runs off the ASGI loop, so unrelated async
        # work still receives execution while the Runtime read is blocked.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

        with pytest.raises(DomainError) as caught:
            await run_blocking(container, lambda _session: "must not run")
        assert caught.value.code == "RUNTIME_READ_SATURATED"

        # Recovery commands have an independent worker, semaphore and DB
        # connection, so a wedged Runtime read cannot deny its own isolation.
        assert await run_blocking_control(container, lambda _session: "control-ready") == (
            "control-ready"
        )

        release.set()
        for _ in range(100):
            if not container.blocking_io_slots.locked():
                break
            await asyncio.sleep(0.001)
        principal_context = contextvars.ContextVar("principal_context", default="missing")
        token = principal_context.set("bound")
        try:
            result = await run_blocking(container, lambda _session: principal_context.get())
        finally:
            principal_context.reset(token)
        assert result == "bound"


@pytest.mark.asyncio
async def test_run_blocking_waits_for_configured_runtime_read_budget() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(_session: _Session) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "released"

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                # Deliberately longer than the former fixed 250ms deadline.
                blocking_pool_timeout_seconds=0.4,
                history_read_pool_size=1,
            ),
        )
        first = asyncio.create_task(run_blocking(container, blocked))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()

        waiting = asyncio.create_task(run_blocking(container, lambda _session: "queued"))
        await asyncio.sleep(0.3)
        assert not waiting.done()
        release.set()
        assert await first == "released"
        assert await waiting == "queued"


@pytest.mark.asyncio
async def test_history_reads_do_not_saturate_interactive_runtime_lane() -> None:
    history_started = threading.Event()
    history_release = threading.Event()

    def history_read(_session: _Session) -> str:
        history_started.set()
        assert history_release.wait(timeout=2)
        return "history"

    with (
        ThreadPoolExecutor(max_workers=1) as interactive_executor,
        ThreadPoolExecutor(max_workers=1) as history_executor,
        ThreadPoolExecutor(max_workers=1) as control_executor,
    ):
        container = SimpleNamespace(
            blocking_executor=interactive_executor,
            blocking_io_slots=asyncio.Semaphore(1),
            history_read_executor=history_executor,
            history_read_slots=asyncio.Semaphore(1),
            blocking_control_executor=control_executor,
            blocking_control_slots=asyncio.Semaphore(1),
            database=_Database(),
            settings=SimpleNamespace(
                blocking_pool_size=1,
                blocking_pool_timeout_seconds=0.05,
                history_read_pool_size=1,
            ),
        )
        history_task = asyncio.create_task(run_blocking_history(container, history_read))
        for _ in range(100):
            if history_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert history_started.is_set()

        # An interactive native-state read must remain available even while an
        # older page is waiting on OpenHands.
        assert await run_blocking(container, lambda _session: "interactive") == "interactive"
        history_release.set()
        assert await history_task == "history"


def test_api_slow_request_log_excludes_query_values(anonymous_client, monkeypatch, caplog) -> None:
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(api_module, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.WARNING, logger=api_module.__name__)

    response = anonymous_client.get(
        "/health?credential=must-not-appear",
        headers={"X-Request-ID": "slow-request-1"},
    )

    assert response.status_code == 200
    record = next(item for item in caplog.records if item.message.startswith("slow API request"))
    assert "route=/health" in record.message
    assert "duration_ms=2000" in record.message
    assert "request_id=slow-request-1" in record.message
    assert "must-not-appear" not in record.message


def test_all_user_message_routes_use_reserved_message_lane() -> None:
    """Initial, normal, and rerun user messages cannot share slow mutations."""

    import ast
    from pathlib import Path

    paths = (
        Path("src/flowweave/modules/agent_workspaces/presentation/router.py"),
        Path("src/flowweave/modules/agent_sessions/presentation/router.py"),
    )
    expected = {
        "agent_workspaces": {
            "create_agent_conversation",
            "agent_message",
            "agent_rerun_edited_message",
        },
        "agent_sessions": {"bootstrap_node_session", "node_session_message", "rerun_node_message"},
    }
    for path in paths:
        module = ast.parse(path.read_text())
        functions = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        group = "agent_workspaces" if "agent_workspaces" in str(path) else "agent_sessions"
        for name in expected[group]:
            calls = [
                call.func.id
                for call in ast.walk(functions[name])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            ]
            assert "run_blocking_message" in calls


def test_cursor_conversation_pages_use_background_history_lane() -> None:
    """Sidebar history pagination must not share the interactive DB lane."""

    import ast
    from pathlib import Path

    module = ast.parse(
        Path("src/flowweave/modules/agent_workspaces/presentation/router.py").read_text()
    )
    route = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_agent_conversations"
    )
    assignments = [
        node
        for node in ast.walk(route)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "execute" for target in node.targets)
    ]
    assert len(assignments) == 1
    lane_choice = assignments[0].value
    assert isinstance(lane_choice, ast.IfExp)
    assert isinstance(lane_choice.body, ast.Name)
    assert isinstance(lane_choice.orelse, ast.Name)
    assert lane_choice.body.id == "run_blocking_history"
    assert lane_choice.orelse.id == "run_blocking"
    assert not any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "run_sync"
        for call in ast.walk(route)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize("budget", [1, 2, 3, 4, 5, 6, 8, 16])
async def test_workspace_reservation_preserves_total_budget_and_fallback(role, budget) -> None:
    container = build_container(
        Settings(_env_file=None, blocking_pool_size=budget, runtime_adapter="mock"), role=role
    )
    try:
        pools = container.database.pool_metrics()
        assert (
            sum(
                pools.get(name, {}).get("size", 0)
                for name in ("blocking", "hydration", "message", "workspace", "lifecycle")
            )
            == budget
        )
        expected = 1 if role == "api" and budget >= 5 else 0
        assert container.workspace_capacity == expected
        if expected:
            assert container.workspace_executor is not container.history_read_executor
            assert container.workspace_io_slots is not container.history_read_slots
            assert container.database.workspace_engine.pool.size() == 1
            assert container.database.workspace_engine.pool._max_overflow == 0
            assert container.hydration_capacity == 2
            assert container.message_capacity == 1
        else:
            assert container.workspace_executor is container.history_read_executor
            assert container.workspace_io_slots is container.history_read_slots
            assert container.database.workspace_sessions is None
        lifecycle = 1 if role == "api" and budget >= 6 else 0
        assert container.lifecycle_capacity == lifecycle
        assert container.blocking_capacity >= 1
        if lifecycle:
            assert container.lifecycle_executor is not container.blocking_executor
            assert container.lifecycle_io_slots is not container.blocking_io_slots
            assert pools["lifecycle"]["size"] == 1
            assert container.database.lifecycle_engine.pool._max_overflow == 0
        else:
            assert container.lifecycle_executor is container.blocking_executor
            assert container.lifecycle_io_slots is container.blocking_io_slots
            assert container.database.lifecycle_sessions is None
    finally:
        await container.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_lane", ["history", "workspace"])
async def test_workspace_and_history_do_not_block_each_other(blocked_lane) -> None:
    """Reproduce the screenshot: stalled native history cannot deny workspace I/O."""
    container = build_container(
        Settings(
            _env_file=None,
            blocking_pool_size=8,
            runtime_adapter="mock",
            blocking_pool_timeout_seconds=0.05,
        ),
        role="api",
    )
    # Keep the actual production executors and SQL pool partition, substituting
    # only sessions: the fault is Runtime/file I/O, not database availability.
    database = _Database()
    database.workspace_sessions = database.blocking_sessions
    resources = container.database
    container.database = database
    started, release = threading.Event(), threading.Event()

    def slow(_session):
        started.set()
        assert release.wait(3)

    blocked = run_blocking_history if blocked_lane == "history" else run_blocking_auxiliary
    independent = run_blocking_auxiliary if blocked_lane == "history" else run_blocking_history
    task = asyncio.create_task(blocked(container, slow))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert await independent(container, lambda _session: "independent") == "independent"
        assert await run_blocking_hydration(container, lambda _session: "hydration") == "hydration"
        assert await run_blocking_message(container, lambda _session: "message") == "message"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Cancellation of the browser request cannot release a still-running thread.
        with pytest.raises(DomainError) as exc:
            await blocked(container, lambda _session: "must wait")
        assert exc.value.code == (
            "RUNTIME_HISTORY_READ_SATURATED"
            if blocked_lane == "history"
            else "RUNTIME_AUXILIARY_SATURATED"
        )
        assert (
            await independent(container, lambda _session: "still independent")
            == "still independent"
        )
        release.set()
        assert await blocked(container, lambda _session: "recovered") == "recovered"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        container.database = resources
        await container.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["agent_workspaces", "agent_sessions"])
async def test_slow_model_route_keeps_interactive_routes_available(host, monkeypatch) -> None:
    import importlib

    router = importlib.import_module(f"flowweave.modules.{host}.presentation.router")
    container = build_container(
        Settings(
            _env_file=None,
            blocking_pool_size=8,
            runtime_adapter="mock",
            blocking_pool_timeout_seconds=0.05,
        ),
        role="api",
    )
    resources = container.database
    database = _Database()
    database.workspace_sessions = database.blocking_sessions
    container.database = database
    started, release = threading.Event(), threading.Event()
    observed = []
    marker = contextvars.ContextVar("lifecycle-test-principal", default="missing")
    token = marker.set("request-principal")
    caller_thread = threading.get_ident()

    def slow_model(session, *args, **kwargs):
        assert threading.get_ident() != caller_thread
        observed.append(marker.get())
        register_commit_action(session, lambda: observed.append("committed"))
        started.set()
        assert release.wait(3)
        return {"model_name": "new-model"}

    payload = SimpleNamespace(
        model_provider_id="provider", model_name="new-model", reasoning_effort=None
    )
    if host == "agent_workspaces":
        monkeypatch.setattr(router.conversations, "switch_conversation_model", slow_model)
        request = router.agent_conversation_model("workspace", "binding", payload, container)
    else:
        monkeypatch.setattr(
            router.agent_sessions.flow_node_conversations,
            "switch_node_conversation_model",
            slow_model,
        )
        request = router.switch_node_session_model("run", "attempt", "binding", payload, container)
    task = asyncio.create_task(request)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        # Ordinary confirmation, formal event reads and all reserved lanes remain available.
        for execute in (
            run_blocking_mutation,
            run_blocking,
            run_blocking_message,
            run_blocking_hydration,
            run_blocking_auxiliary,
            run_blocking_history,
            run_blocking_control,
        ):
            assert await asyncio.wait_for(execute(container, lambda _db: "ready"), 0.5) == "ready"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Cancelled HTTP response leaves the accepted write running exactly once.
        with pytest.raises(DomainError, match="busy") as exc:
            await run_blocking_lifecycle(container, lambda _db: observed.append("unexpected"))
        assert exc.value.code == "RUNTIME_LIFECYCLE_SATURATED"
        release.set()
        assert await run_blocking_lifecycle(container, lambda _db: "next") == "next"
        assert observed == ["request-principal", "committed"]
    finally:
        marker.reset(token)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        container.database = resources
        await container.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [4, 8])
async def test_lifecycle_queue_cancel_and_rollback_release_real_capacity(budget) -> None:
    container = build_container(
        Settings(
            _env_file=None,
            blocking_pool_size=budget,
            runtime_adapter="mock",
            blocking_pool_timeout_seconds=0.1,
        ),
        role="api",
    )
    resources = container.database
    container.database = _Database()
    started, release = threading.Event(), threading.Event()
    actions = []

    def failed_write(session):
        register_rollback_action(session, lambda: actions.append("rollback"))
        register_commit_action(session, lambda: actions.append("must not commit"))
        started.set()
        assert release.wait(3)
        raise DomainError("MODEL_REJECTED", "model rejected", 409)

    first = asyncio.create_task(run_blocking_lifecycle(container, failed_write))
    queued = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(
            run_blocking_lifecycle(container, lambda _db: actions.append("must not start"))
        )
        await asyncio.sleep(0)  # Let the waiter enter admission before cancelling it.
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        if budget == 4:
            # The fallback must share ordinary mutation admission even though
            # it has no reserved executor or SQL connection.
            with pytest.raises(DomainError) as exc:
                await run_blocking_mutation(container, lambda _db: "blocked")
            assert exc.value.code == "RUNTIME_MUTATION_SATURATED"
        else:
            assert (
                await run_blocking_mutation(container, lambda _db: "interactive") == "interactive"
            )
        release.set()
        with pytest.raises(DomainError) as exc:
            await first
        assert exc.value.code == "MODEL_REJECTED"
        assert await run_blocking_lifecycle(container, lambda _db: "recovered") == "recovered"
        assert actions == ["rollback"]
    finally:
        release.set()
        await asyncio.gather(first, *([queued] if queued else []), return_exceptions=True)
        container.database = resources
        await container.close()


def test_audited_lifecycle_routes_keep_confirmation_and_message_lanes_separate() -> None:
    import ast
    from pathlib import Path

    expected = {
        "agent_workspaces": {
            "patch_agent_conversation",
            "add_agent_conversation_capability",
            "synchronize_agent_conversation_credentials",
            "delete_agent_conversation",
            "agent_conversation_model",
            "agent_streaming_migration",
            "agent_fork_conversation",
        },
        "agent_sessions": {
            "node_session_host",
            "add_node_session_capability",
            "synchronize_node_session_credentials",
            "create_node_session",
            "delete_node_session",
            "switch_node_session_model",
            "fork_node_session",
        },
    }
    for module, names in expected.items():
        tree = ast.parse(Path(f"src/flowweave/modules/{module}/presentation/router.py").read_text())
        mapped = {
            node.name
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "run_blocking_lifecycle"
                for call in ast.walk(node)
            )
        }
        assert mapped == names


@pytest.mark.asyncio
async def test_lifecycle_can_execute_when_all_ordinary_read_slots_are_occupied() -> None:
    container = build_container(
        Settings(
            _env_file=None,
            blocking_pool_size=8,
            runtime_adapter="mock",
            blocking_pool_timeout_seconds=0.05,
        ),
        role="api",
    )
    resources = container.database
    container.database = _Database()
    release = threading.Event()
    started = [threading.Event() for _ in range(container.blocking_capacity)]

    def read(index):
        def operation(_session):
            started[index].set()
            assert release.wait(3)

        return operation

    tasks = [asyncio.create_task(run_blocking(container, read(i))) for i in range(len(started))]
    try:
        for signal in started:
            assert await asyncio.to_thread(signal.wait, 1)
        assert await run_blocking_lifecycle(container, lambda _db: "lifecycle") == "lifecycle"
        with pytest.raises(DomainError) as exc:
            await run_blocking(container, lambda _db: "saturated")
        assert exc.value.code == "RUNTIME_READ_SATURATED"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        container.database = resources
        await container.close()
