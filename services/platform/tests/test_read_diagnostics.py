from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from flowweave.bootstrap.api import create_app
from flowweave.bootstrap.settings import Settings
from flowweave.shared.errors import DomainError
from flowweave.shared.http import run_blocking


@pytest.fixture(autouse=True)
def database():
    """These public diagnostic routes exercise real threads, without connecting SQL."""
    yield


@pytest.fixture
def diagnostic_app(monkeypatch):
    # Alembic's fileConfig disables existing application loggers when an earlier
    # database fixture migrates. Restore only the loggers asserted by this test.
    for name in ("flowweave.shared.read_diagnostics", "flowweave.bootstrap.api"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    app = create_app(Settings())
    try:
        yield app
    finally:
        asyncio.run(app.state.container.close())


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            DomainError("RUNTIME_READ_PER_RUNTIME_SATURATED", "private failure text", 503),
            503,
            "RUNTIME_READ_PER_RUNTIME_SATURATED",
        ),
        (
            DomainError("AGENT_RUNTIME_UNAVAILABLE", "private failure text", 503),
            503,
            "AGENT_RUNTIME_UNAVAILABLE",
        ),
        (PoolTimeout("private failure text"), 500, "DATABASE_POOL_TIMEOUT"),
        (RuntimeError("private connection URL"), 500, "UNEXPECTED_READ_FAILURE"),
        (
            OperationalError(
                "private SQL text",
                {"key": "private parameter"},
                psycopg.OperationalError("private connection URL"),
            ),
            500,
            "DATABASE_CONNECTION_ERROR",
        ),
        *[
            (
                OperationalError(
                    "private SQL text",
                    {"key": "private parameter"},
                    error_type("private failure text"),
                ),
                500,
                code,
            )
            for error_type, code in (
                (psycopg.errors.QueryCanceled, "DATABASE_STATEMENT_TIMEOUT"),
                (psycopg.errors.LockNotAvailable, "DATABASE_LOCK_TIMEOUT"),
                (psycopg.errors.DeadlockDetected, "DATABASE_DEADLOCK"),
            )
        ],
    ],
)
def test_read_failure_correlates_worker_and_http_without_private_values(
    diagnostic_app,
    caplog,
    error,
    status,
    code,
):
    caplog.set_level(logging.WARNING)
    app = diagnostic_app

    @app.get("/health/diagnostic/{target}")
    async def probe(target: str):
        def fail(_db):
            raise error

        return await run_blocking(app.state.container, fail)

    request_id = "c3d4b399-b035-4ad3-81b4-e56426b017b6"
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.get(
            "/health/diagnostic/private-target?secret=private-query",
            headers={"X-Request-ID": request_id},
        )
    finally:
        client.close()
    assert response.status_code == status
    assert response.json()["error"]["request_id"] == request_id
    records = [
        record
        for record in caplog.records
        if record.message.startswith(
            ("Runtime read failed", "API request failed", "unhandled API exception")
        )
    ]
    assert len(records) == 2
    assert all(f"request_id={request_id}" in record.message for record in records)
    assert all(f"error_code={code}" in record.message for record in records)
    assert "lane=read phase=execution" in records[0].message
    assert "route=/health/diagnostic/{target}" in records[1].message
    assert all(record.exc_info is None for record in records)
    for value in (
        "private failure text",
        "private SQL text",
        "private parameter",
        "private connection URL",
        "private-target",
        "private-query",
    ):
        assert value not in caplog.text


def test_request_id_does_not_echo_an_untrusted_header(diagnostic_app, caplog):
    caplog.set_level(logging.WARNING)
    app = diagnostic_app

    @app.get("/health/id-failure")
    async def fail():
        raise DomainError("AGENT_RUNTIME_UNAVAILABLE", "retry", 503)

    client = TestClient(app)
    try:
        responses = [
            client.get("/health/id-failure", headers={"X-Request-ID": value})
            for value in ("https://private.invalid/?key=sk-private", "bad\tlog-value")
        ]
        responses.append(client.get("/health/id-failure"))
    finally:
        client.close()
    ids = [response.json()["error"]["request_id"] for response in responses]
    assert len(set(ids)) == 3
    assert all(str(UUID(value)) == value for value in ids)
    assert "private.invalid" not in caplog.text
    assert "sk-private" not in caplog.text
    assert "log-value" not in caplog.text


def test_saturated_read_logs_same_request_and_safe_phase(diagnostic_app, caplog):
    caplog.set_level(logging.WARNING)
    app = diagnostic_app
    app.state.container.settings = app.state.container.settings.model_copy(
        update={"blocking_pool_timeout_seconds": 0.01}
    )
    app.state.container.blocking_io_slots = asyncio.Semaphore(0)

    @app.get("/health/saturated")
    async def saturated():
        return await run_blocking(app.state.container, lambda _db: "not reached")

    client = TestClient(app)
    try:
        response = client.get("/health/saturated")
    finally:
        client.close()
    assert response.status_code == 503
    request_id = response.json()["error"]["request_id"]
    records = [
        record.message
        for record in caplog.records
        if record.message.startswith(("Runtime read failed", "API request failed"))
    ]
    assert len(records) == 2
    assert all(f"request_id={request_id}" in record for record in records)
    assert "lane=read phase=admission" in records[0]
    assert all("error_code=RUNTIME_READ_SATURATED" in record for record in records)
