"""Validate the rendered local Compose PostgreSQL connection budget."""

from __future__ import annotations

import json
import sys
from typing import Any, cast


def fail(message: str) -> None:
    raise SystemExit(f"compose capacity check failed: {message}")


def mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{name} must be an object")
    return cast(dict[str, Any], value)


def environment(service: dict[str, Any], name: str) -> dict[str, str]:
    raw = mapping(service.get("environment", {}), f"{name} environment")
    return {str(key): str(value) for key, value in raw.items()}


def integer(values: dict[str, str], name: str, service: str, *, minimum: int) -> int:
    raw = values.get(name)
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        value = None
    if value is None or value < minimum:
        fail(f"{service} {name} must be an integer of at least {minimum}")
    return value


def worker_processes(service: dict[str, Any], name: str) -> int:
    command = service.get("command", [])
    if not isinstance(command, list):
        fail(f"{name} command must be a list")
    arguments = [str(value) for value in command]
    if "--workers" not in arguments:
        return 1
    index = arguments.index("--workers") + 1
    if index == len(arguments):
        fail(f"{name} --workers requires a value")
    try:
        workers = int(arguments[index])
    except ValueError:
        workers = 0
    if workers < 1:
        fail(f"{name} --workers must be a positive integer")
    return workers


def process_connection_limit(values: dict[str, str], name: str, *, worker: bool) -> int:
    pool_size = integer(values, "POOL_SIZE", name, minimum=1)
    overflow = integer(values, "POOL_MAX_OVERFLOW", name, minimum=0)
    if overflow != 0:
        fail(f"{name} POOL_MAX_OVERFLOW must be 0 for a deterministic budget")
    blocking = integer(values, "BLOCKING_POOL_SIZE", name, minimum=1)
    history = integer(values, "HISTORY_READ_POOL_SIZE", name, minimum=1)
    poll = integer(values, "RUNTIME_POLL_WORKER_CONCURRENCY", name, minimum=1) if worker else 0
    # Database owns an async, blocking, history and control engine. The Worker
    # additionally owns an isolated poll pool. The control engine also inherits
    # overflow, which is required to remain zero above.
    return pool_size + blocking + history + poll + 1


def check_document(document: dict[str, Any]) -> None:
    services = mapping(document.get("services"), "services")
    required = {"runtime-provider", "api", "stream-api", "worker"}
    if missing := required - services.keys():
        fail(f"missing services: {', '.join(sorted(missing))}")

    runtime_provider = mapping(services["runtime-provider"], "runtime-provider")
    if "DATABASE_URL" in environment(runtime_provider, "runtime-provider"):
        fail("runtime-provider must not receive DATABASE_URL")

    total = 0
    limits: set[int] = set()
    reserves: set[int] = set()
    for name in ("api", "stream-api", "worker"):
        service = mapping(services[name], name)
        values = environment(service, name)
        processes = worker_processes(service, name)
        total += processes * process_connection_limit(values, name, worker=name == "worker")
        limits.add(integer(values, "POSTGRES_CONNECTION_LIMIT", name, minimum=1))
        reserves.add(integer(values, "DATABASE_CONNECTION_RESERVE", name, minimum=1))

    if len(limits) != 1 or len(reserves) != 1:
        fail("api, stream-api, and worker must declare the same database budget")
    limit = limits.pop()
    reserve = reserves.pop()
    if reserve >= limit:
        fail("DATABASE_CONNECTION_RESERVE must be smaller than POSTGRES_CONNECTION_LIMIT")
    if total > limit - reserve:
        fail(
            f"steady-state connection limit {total} exceeds budget {limit - reserve} "
            f"(PostgreSQL limit {limit} minus reserve {reserve})"
        )


def main() -> None:
    try:
        document = mapping(json.load(sys.stdin), "Compose document")
    except json.JSONDecodeError as exc:
        fail(f"invalid rendered JSON: {exc}")
    check_document(document)
    print("compose capacity check passed")


if __name__ == "__main__":
    main()
