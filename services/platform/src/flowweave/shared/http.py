from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Annotated, TypeVar, cast

from fastapi import Depends, Header, WebSocket, WebSocketException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from flowweave.bootstrap.container import Container
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.security import (
    bind_principal,
    current_principal,
    reset_principal,
)
from flowweave.runtime.read_budget import formal_response_budget, hydration_time_left
from flowweave.shared.application.transactions import (
    mark_uow_owned,
    run_commit_actions,
    run_rollback_actions,
)
from flowweave.shared.errors import DomainError

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = TypeVar("P")
R = TypeVar("R")


def get_container(connection: HTTPConnection) -> Container:
    """Resolve the application container for both HTTP and WebSocket scopes."""

    return connection.app.state.container


async def require_authenticated_connection(
    connection: HTTPConnection,
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[None]:
    """Authenticate WebSockets; HTTP requests are already bound by middleware."""

    if current_principal() is not None:
        yield
        return
    token = connection.cookies.get(users.SESSION_COOKIE)
    async with container.database.session() as session:
        principal = await session.run_sync(lambda db: users.authenticate(db, token))
        if principal is not None:
            await session.commit()
    if principal is None:
        if connection.scope["type"] == "websocket":
            await cast(WebSocket, connection).accept()
            raise WebSocketException(code=4401, reason="请先登录")
        raise DomainError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    principal_token = bind_principal(principal)
    try:
        yield
    finally:
        reset_principal(principal_token)


async def require_agent_session_access(connection: HTTPConnection) -> None:
    principal = current_principal()
    if principal is not None and (principal.is_super_admin or principal.can_use_agent_sessions):
        return
    if connection.scope["type"] == "websocket":
        await cast(WebSocket, connection).accept()
        raise WebSocketException(code=4403, reason="当前账号未开通 Agent 会话")
    raise DomainError("AGENT_SESSION_ACCESS_REQUIRED", "当前账号未开通 Agent 会话", 403)


async def get_db(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    async with container.database.uow() as uow:
        mark_uow_owned(uow.session.sync_session)
        yield uow.session


async def run_sync(db: AsyncSession, operation: Callable[[Session], T]) -> T:
    """Execute and commit one synchronous application command in the async UoW."""

    try:
        result = await db.run_sync(operation)
        await db.commit()
    except BaseException:
        await db.rollback()
        await db.run_sync(run_rollback_actions)
        raise
    await db.run_sync(run_commit_actions)
    return result


async def run_blocking(container: Container, operation: Callable[[Session], T]) -> T:
    """Run a synchronous DB/Runtime read in a bounded worker thread.

    Several compatibility services perform synchronous OpenHands HTTP calls while
    holding a SQLAlchemy session. Running them through ``AsyncSession.run_sync``
    blocks the ASGI loop. A stalled conversation could therefore delay unrelated
    authentication and control-plane requests. This helper uses a separate,
    non-overflowing DB pool and a matching semaphore so both threads and database
    connections have a hard process-local ceiling.
    """

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.blocking_executor,
        slots=container.blocking_io_slots,
        session_factory=container.database.blocking_sessions,
        saturation_code="RUNTIME_READ_SATURATED",
        saturation_message="Agent Runtime reads are busy; retry shortly",
        lane_name="read",
        active_limit=getattr(container, "blocking_capacity", container.settings.blocking_pool_size),
    )


async def run_blocking_mutation(container: Container, operation: Callable[[Session], T]) -> T:
    """Offload a Runtime write without taking every interactive read slot."""

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.blocking_executor,
        slots=container.blocking_io_slots,
        admission_slots=container.blocking_mutation_slots,
        session_factory=container.database.blocking_sessions,
        saturation_code="RUNTIME_MUTATION_SATURATED",
        saturation_message="Agent Runtime writes are busy; retry shortly",
        lane_name="mutation",
        active_limit=min(2, max(1, container.blocking_capacity // 2))
        if hasattr(container, "blocking_capacity")
        else min(2, max(1, container.settings.blocking_pool_size // 2)),
    )


async def run_blocking_lifecycle(container: Container, operation: Callable[[Session], T]) -> T:
    """Isolate slow native lifecycle mutations without changing write semantics.

    Admission is bounded before executing the operation. Once submitted, a
    cancelled caller cannot undo the write or free its real thread/SQL capacity;
    _run_blocking_lane holds that capacity through commit/rollback callbacks.
    Small configurations share the existing mutation admission, not a new queue.
    """

    if not container.lifecycle_capacity:
        return await run_blocking_mutation(container, operation)
    sessions = container.database.lifecycle_sessions
    if sessions is None:
        raise RuntimeError("Lifecycle reservation requires its dedicated database pool")
    return await _run_blocking_lane(
        container,
        operation,
        executor=container.lifecycle_executor,
        slots=container.lifecycle_io_slots,
        session_factory=sessions,
        saturation_code="RUNTIME_LIFECYCLE_SATURATED",
        saturation_message="Agent lifecycle changes are busy; retry shortly",
        lane_name="lifecycle",
        active_limit=container.lifecycle_capacity,
    )


async def run_blocking_message(container: Container, operation: Callable[[Session], T]) -> T:
    """Run latency-sensitive user message delivery on its reserved API lane."""

    sessions = container.database.message_sessions or container.database.blocking_sessions
    return await _run_blocking_lane(
        container,
        operation,
        executor=container.message_executor,
        slots=container.message_io_slots,
        session_factory=sessions,
        saturation_code="RUNTIME_MESSAGE_SATURATED",
        saturation_message="Agent message delivery is busy; retry shortly",
        lane_name="message",
        active_limit=container.message_capacity or container.blocking_capacity,
    )


async def run_blocking_hydration(container: Container, operation: Callable[[Session], T]) -> T:
    """Give cache-key lookup and formal first-screen reads reserved API capacity."""

    def within_budget(session: Session) -> T:
        hydration_time_left()
        result = operation(session)
        hydration_time_left()
        return result

    sessions = container.database.hydration_sessions or container.database.blocking_sessions
    return await _run_blocking_lane(
        container,
        within_budget,
        executor=container.hydration_executor,
        slots=container.hydration_io_slots,
        session_factory=sessions,
        saturation_code="RUNTIME_READ_SATURATED",
        saturation_message="Agent Runtime first-screen reads are busy; retry shortly",
        lane_name="hydration",
        active_limit=container.hydration_capacity or container.blocking_capacity,
        wait_timeout=hydration_time_left(),
    )


async def observe_hydration_phase(container: Container, phase: str, operation: Awaitable[T]) -> T:
    """Record one bounded hydration phase without high-cardinality labels."""

    started_at = asyncio.get_running_loop().time()
    outcome = "error"
    try:
        result = await operation
        outcome = "ok"
        return result
    finally:
        metrics = getattr(container, "metrics", None)
        if metrics is not None:
            metrics.observe_operation(
                f"agent_session.hydration.{phase}",
                asyncio.get_running_loop().time() - started_at,
                outcome=outcome,
            )


async def run_hydration_runtime(container: Container, operation: Callable[[], T]) -> T:
    """Run a formal hydration Runtime read without retaining a DB connection.

    Hydration first resolves and fences the binding in a short database phase.
    The potentially slow OpenHands read then retains only the hydration executor
    slot, Runtime bulkhead, and request deadline. A second short DB phase
    projects the returned formal events after revalidating the frozen identity.
    """

    started_at = monotonic()
    metrics = getattr(container, "metrics", None)
    outcome = "error"
    try:
        remaining = hydration_time_left()
        await asyncio.wait_for(
            container.hydration_io_slots.acquire(),
            timeout=min(container.settings.blocking_pool_timeout_seconds, remaining)
            if remaining is not None
            else container.settings.blocking_pool_timeout_seconds,
        )
        outcome = "ok"
    except TimeoutError as exc:
        hydration_time_left()
        logger.warning(
            "hydration Runtime executor saturated active_limit=%d",
            container.hydration_capacity or container.blocking_capacity,
        )
        raise DomainError(
            "RUNTIME_READ_SATURATED",
            "Agent Runtime first-screen reads are busy; retry shortly",
            503,
        ) from exc
    finally:
        if metrics is not None:
            metrics.observe_operation(
                "runtime_api.hydration_runtime.admission_wait",
                monotonic() - started_at,
                outcome=outcome,
            )

    submitted_at = monotonic()

    def execute() -> T:
        started_at = monotonic()
        outcome = "error"
        if metrics is not None:
            metrics.observe_operation(
                "runtime_api.hydration_runtime.executor_wait",
                started_at - submitted_at,
                outcome="ok",
            )
        try:
            hydration_time_left()
            result = operation()
            hydration_time_left()
            outcome = "ok"
            return result
        finally:
            if metrics is not None:
                metrics.observe_operation(
                    "runtime_api.hydration_runtime.worker_duration",
                    monotonic() - started_at,
                    outcome=outcome,
                )

    context = contextvars.copy_context()
    try:
        worker = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(
                container.hydration_executor,
                context.run,
                execute,
            )
        )
    except BaseException:
        container.hydration_io_slots.release()
        raise

    def release_slot(completed: asyncio.Future[T]) -> None:
        container.hydration_io_slots.release()
        if not completed.cancelled():
            completed.exception()

    worker.add_done_callback(release_slot)
    return await asyncio.shield(worker)


async def run_blocking_control(container: Container, operation: Callable[[Session], T]) -> T:
    """Run an Agent Runtime control command on its reserved recovery lane."""

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.blocking_control_executor,
        slots=container.blocking_control_slots,
        session_factory=container.database.control_sessions,
        saturation_code="RUNTIME_CONTROL_SATURATED",
        saturation_message="Agent Runtime recovery is already in progress",
        lane_name="control",
        active_limit=1,
    )


async def run_blocking_history(container: Container, operation: Callable[[Session], T]) -> T:
    """Run one best-effort older-history page outside the interactive lane.

    A browser may prefetch many historical OpenHands pages after it has painted
    the latest window. This lane is intentionally small and independently
    pooled: saturation drops the prefetch rather than delaying readiness or
    confirmation reads for a running conversation. Workspace/Git operations
    use their own reservation when the configured blocking budget permits it.
    """

    return await _run_blocking_lane(
        container,
        operation,
        executor=container.history_read_executor,
        slots=container.history_read_slots,
        session_factory=container.database.history_sessions,
        saturation_code="RUNTIME_HISTORY_READ_SATURATED",
        saturation_message="Conversation history is being loaded; retry shortly",
        lane_name="history",
        active_limit=container.settings.history_read_pool_size,
    )


async def run_formal_events(
    container: Container,
    prepare: Callable[[Session], P],
    read: Callable[[P], R],
    project: Callable[[Session, P, R], T],
    *,
    history: bool,
) -> T:
    """Keep one real worker slot, but close DB sessions during Runtime I/O."""

    session_factory = (
        container.database.history_sessions if history else container.database.blocking_sessions
    )

    def execute() -> T:
        prepared = _observe_event_phase(
            container, "prepare_db", lambda: _run_session_operation(session_factory, prepare)
        )
        snapshot = _observe_event_phase(container, "runtime_read", lambda: read(prepared))
        # Cancellation cannot stop the thread. Its copied deadline must reject
        # late snapshots before opening another DB session or projecting them.
        hydration_time_left()
        return _observe_event_phase(
            container,
            "project_db",
            lambda: _run_session_operation(
                session_factory, lambda session: project(session, prepared, snapshot)
            ),
        )

    started_at = monotonic()
    outcome = "error"
    try:
        async with formal_response_budget(container.settings.runtime_event_read_timeout_seconds):
            result = await _run_blocking_operation(
                container,
                execute,
                executor=container.history_read_executor
                if history
                else container.blocking_executor,
                slots=container.history_read_slots if history else container.blocking_io_slots,
                saturation_code="RUNTIME_HISTORY_READ_SATURATED"
                if history
                else "RUNTIME_READ_SATURATED",
                saturation_message="Conversation history is being loaded; retry shortly"
                if history
                else "Agent Runtime reads are busy; retry shortly",
                lane_name="history" if history else "read",
                active_limit=container.settings.history_read_pool_size
                if history
                else getattr(container, "blocking_capacity", container.settings.blocking_pool_size),
            )
            outcome = "ok"
            return result
    finally:
        metrics = getattr(container, "metrics", None)
        if metrics is not None:
            metrics.observe_operation(
                "agent_session.events.history_response"
                if history
                else "agent_session.events.response",
                monotonic() - started_at,
                outcome=outcome,
            )


def _observe_event_phase(container: Container, phase: str, operation: Callable[[], T]) -> T:
    started_at = monotonic()
    outcome = "error"
    try:
        hydration_time_left()
        result = operation()
        hydration_time_left()
        outcome = "ok"
        return result
    finally:
        metrics = getattr(container, "metrics", None)
        if metrics is not None:
            metrics.observe_operation(
                f"agent_session.events.{phase}", monotonic() - started_at, outcome=outcome
            )


async def acquire_terminal_slot(container: Container) -> None:
    """Reserve one bounded terminal stream slot for a WebSocket lifetime."""

    try:
        await asyncio.wait_for(
            container.terminal_slots.acquire(),
            timeout=container.settings.blocking_pool_timeout_seconds,
        )
    except TimeoutError as exc:
        logger.warning(
            "terminal stream pool saturated active_limit=%d",
            container.settings.terminal_stream_pool_size,
        )
        raise DomainError(
            "RUNTIME_TERMINAL_SATURATED",
            "Terminal capacity is busy; retry shortly",
            503,
        ) from exc


def release_terminal_slot(container: Container) -> None:
    """Release a terminal stream slot after its terminal has been closed."""

    container.terminal_slots.release()


async def run_terminal_control(container: Container, operation: Callable[[], T]) -> T:
    """Run short terminal setup/control I/O off the default asyncio executor."""

    return await _run_executor_operation(container.terminal_control_executor, operation)


async def run_terminal_stream(container: Container, operation: Callable[[], T]) -> T:
    """Run a potentially blocking terminal read on its dedicated stream pool."""

    return await _run_executor_operation(container.terminal_stream_executor, operation)


async def _run_executor_operation(executor: ThreadPoolExecutor, operation: Callable[[], T]) -> T:
    """Offload non-DB I/O while preserving request context in a named pool."""

    context = contextvars.copy_context()
    worker = asyncio.ensure_future(
        asyncio.get_running_loop().run_in_executor(executor, context.run, operation)
    )

    def consume_exception(completed: asyncio.Future[T]) -> None:
        if not completed.cancelled():
            completed.exception()

    worker.add_done_callback(consume_exception)
    return await asyncio.shield(worker)


async def run_blocking_admin(container: Container, operation: Callable[[Session], T]) -> T:
    """Run an operator diagnostic/control database action on its own lane."""

    admin_sessions = container.database.admin_sessions
    if admin_sessions is None:
        raise RuntimeError("Admin execution requires the API admin database pool")
    return await _run_blocking_lane(
        container,
        operation,
        executor=container.admin_executor,
        slots=container.admin_io_slots,
        session_factory=admin_sessions,
        saturation_code="ADMIN_CONTROL_SATURATED",
        saturation_message="Administrator control work is busy; retry shortly",
        lane_name="admin",
        active_limit=1,
    )


async def run_blocking_auxiliary(container: Container, operation: Callable[[Session], T]) -> T:
    """Run filesystem/Git I/O independently of slow native history requests.

    The reservation partitions the existing blocking SQL budget. Small API
    configurations without room for a reservation share the *same* bounded
    history semaphore, executor and SQL pool, never a second admission limit.
    """

    sessions = container.database.workspace_sessions or container.database.history_sessions
    return await _run_blocking_lane(
        container,
        operation,
        executor=container.workspace_executor,
        slots=container.workspace_io_slots,
        session_factory=sessions,
        saturation_code="RUNTIME_AUXILIARY_SATURATED",
        saturation_message="Workspace operations are busy; retry shortly",
        lane_name="workspace",
        active_limit=container.workspace_capacity or container.settings.history_read_pool_size,
    )


async def _run_blocking_lane(
    container: Container,
    operation: Callable[[Session], T],
    *,
    executor: ThreadPoolExecutor,
    slots: asyncio.Semaphore,
    session_factory: Callable[[], Session],
    saturation_code: str,
    saturation_message: str,
    lane_name: str,
    active_limit: int,
    admission_slots: asyncio.Semaphore | None = None,
    wait_timeout: float | None = None,
) -> T:
    return await _run_blocking_operation(
        container,
        lambda: _run_session_operation(session_factory, operation),
        executor=executor,
        slots=slots,
        saturation_code=saturation_code,
        saturation_message=saturation_message,
        lane_name=lane_name,
        active_limit=active_limit,
        admission_slots=admission_slots,
        wait_timeout=wait_timeout,
    )


def _run_session_operation(
    session_factory: Callable[[], Session], operation: Callable[[Session], T]
) -> T:
    hydration_time_left()
    with session_factory() as session:
        mark_uow_owned(session)
        try:
            result = operation(session)
            hydration_time_left()
            session.commit()
        except BaseException:
            session.rollback()
            run_rollback_actions(session)
            raise
        run_commit_actions(session)
        return result


async def _run_blocking_operation(
    container: Container,
    operation: Callable[[], T],
    *,
    executor: ThreadPoolExecutor,
    slots: asyncio.Semaphore,
    saturation_code: str,
    saturation_message: str,
    lane_name: str,
    active_limit: int,
    admission_slots: asyncio.Semaphore | None = None,
    wait_timeout: float | None = None,
) -> T:
    admitted = False
    metrics = getattr(container, "metrics", None)

    async def acquire(semaphore: asyncio.Semaphore) -> None:
        remaining = hydration_time_left()
        timeout = container.settings.blocking_pool_timeout_seconds
        if wait_timeout is not None:
            timeout = min(timeout, wait_timeout)
        if remaining is not None:
            timeout = min(timeout, remaining)
        await asyncio.wait_for(semaphore.acquire(), timeout=timeout)

    started_at = monotonic()
    admission_outcome = "error"
    try:
        if admission_slots is not None:
            await acquire(admission_slots)
            admitted = True
        # Runtime reads are deliberately bounded, but ordinary concurrent
        # hydration must be allowed to wait for the configured DB/Runtime
        # budget.  A former fixed 250ms deadline bypassed
        # BLOCKING_POOL_TIMEOUT_SECONDS and turned normal short reads into
        # misleading 503 saturation responses.
        await acquire(slots)
        admission_outcome = "ok"
    except TimeoutError as exc:
        if admitted and admission_slots is not None:
            admission_slots.release()
        hydration_time_left()
        logger.warning(
            "blocking Runtime %s pool saturated active_limit=%d",
            lane_name,
            active_limit,
        )
        raise DomainError(
            saturation_code,
            saturation_message,
            503,
        ) from exc
    except BaseException:
        if admitted and admission_slots is not None:
            admission_slots.release()
        raise
    finally:
        if metrics is not None:
            metrics.observe_operation(
                f"runtime_api.{lane_name}.admission_wait",
                monotonic() - started_at,
                outcome=admission_outcome,
            )

    submitted_at = monotonic()

    def execute() -> T:
        started_at = monotonic()
        outcome = "error"
        if metrics is not None:
            metrics.observe_operation(
                f"runtime_api.{lane_name}.executor_wait", started_at - submitted_at, outcome="ok"
            )
        try:
            hydration_time_left()
            result = operation()
            hydration_time_left()
            outcome = "ok"
            return result
        finally:
            if metrics is not None:
                metrics.observe_operation(
                    f"runtime_api.{lane_name}.worker_duration",
                    monotonic() - started_at,
                    outcome=outcome,
                )

    context = contextvars.copy_context()
    try:
        worker = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(
                executor,
                context.run,
                execute,
            )
        )
    except BaseException:
        slots.release()
        if admitted and admission_slots is not None:
            admission_slots.release()
        raise

    def release_slot(completed: asyncio.Future[T]) -> None:
        slots.release()
        if admitted and admission_slots is not None:
            admission_slots.release()
        # A disconnected HTTP client cancels the request coroutine, but Python
        # cannot stop an already-running thread. Consume its eventual exception
        # and release capacity only after its bounded Runtime call has exited.
        if not completed.cancelled():
            completed.exception()

    worker.add_done_callback(release_slot)
    return await asyncio.shield(worker)


Db = Annotated[AsyncSession, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def command_key(value: str | None, *, fallback: str) -> str:
    return value or fallback
