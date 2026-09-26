from __future__ import annotations

import runpy
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

_CHECKER = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "compose_capacity_check.py"))
check_document = _CHECKER["check_document"]


def _environment(
    pool_size: int,
    blocking_pool_size: int,
    history_pool_size: int,
    *,
    poll_pool_size: int = 1,
) -> dict[str, str]:
    return {
        "POOL_SIZE": str(pool_size),
        "POOL_MAX_OVERFLOW": "0",
        "BLOCKING_POOL_SIZE": str(blocking_pool_size),
        "HISTORY_READ_POOL_SIZE": str(history_pool_size),
        "RUNTIME_POLL_WORKER_CONCURRENCY": str(poll_pool_size),
        "POSTGRES_CONNECTION_LIMIT": "100",
        "DATABASE_CONNECTION_RESERVE": "20",
    }


def _document() -> dict[str, Any]:
    return {
        "services": {
            "runtime-provider": {"environment": {}},
            "api": {
                "command": ["uvicorn", "app", "--workers", "4"],
                "environment": _environment(4, 3, 1),
            },
            "stream-api": {
                "command": ["uvicorn", "app", "--workers", "4"],
                "environment": _environment(2, 1, 1),
            },
            "worker": {"command": ["python", "-m", "worker"], "environment": _environment(4, 4, 1)},
        }
    }


def test_compose_capacity_check_accepts_reserved_connection_budget() -> None:
    check_document(_document())


def _increase_api_blocking_pool(document: dict[str, Any]) -> None:
    document["services"]["api"]["environment"]["BLOCKING_POOL_SIZE"] = "7"


def _enable_pool_overflow(document: dict[str, Any]) -> None:
    document["services"]["worker"]["environment"]["POOL_MAX_OVERFLOW"] = "1"


def _leak_database_url_to_runtime_provider(document: dict[str, Any]) -> None:
    document["services"]["runtime-provider"]["environment"]["DATABASE_URL"] = (
        "postgresql://example.invalid/db"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (_increase_api_blocking_pool, "exceeds budget"),
        (_enable_pool_overflow, "must be 0"),
        (_leak_database_url_to_runtime_provider, "must not receive DATABASE_URL"),
    ),
)
def test_compose_capacity_check_rejects_unsafe_topology(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = deepcopy(_document())
    mutate(document)

    with pytest.raises(SystemExit, match=message):
        check_document(document)


def test_compose_capacity_check_counts_worker_poll_pool() -> None:
    document = _document()
    document["services"]["worker"]["environment"]["RUNTIME_POLL_WORKER_CONCURRENCY"] = "20"

    with pytest.raises(SystemExit, match="exceeds budget"):
        check_document(document)
