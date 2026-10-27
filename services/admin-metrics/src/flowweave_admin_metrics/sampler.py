from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import httpx
import psycopg
from psycopg.rows import dict_row

from flowweave_admin_metrics.settings import Settings

_LOCK_KEY = 8_142_661


def collect_once(settings: Settings) -> bool:
    observations = _observations(settings)
    samples = _samples(observations)
    if not samples:
        return False
    with psycopg.connect(
        settings.normalized_database_url,
        row_factory=cast(Any, dict_row),
        autocommit=True,
    ) as connection:
        database = cast(Any, connection)
        locked = database.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,)).fetchone()
        if not locked or not locked["pg_try_advisory_lock"]:
            return False
        try:
            observed_at = datetime.now().astimezone()
            database.executemany(
                """
                INSERT INTO admin_metric_samples (observed_at, scope, subject, metric, value)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [(observed_at, *sample) for sample in samples],
            )
            connection.execute(
                "DELETE FROM admin_metric_samples "
                "WHERE observed_at < now() - (%s * interval '1 day')",
                (settings.admin_metrics_retention_days,),
            )
            return True
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))



_CLEANUP_LOCK_KEY = 8_142_662


def cleanup_expired_temporary_services(settings: Settings) -> int:
    """Stop listeners older than the Admin retention limit in managed Runtime containers."""

    with psycopg.connect(
        settings.normalized_database_url,
        row_factory=cast(Any, dict_row),
        autocommit=True,
    ) as connection:
        database = cast(Any, connection)
        locked = database.execute(
            "SELECT pg_try_advisory_lock(%s)", (_CLEANUP_LOCK_KEY,)
        ).fetchone()
        if not locked or not locked["pg_try_advisory_lock"]:
            return 0
        try:
            stopped = 0
            for resource in _managed_runtime_resources(_observations(settings)):
                for service in _runtime_services(settings, resource):
                    age = service.get("age_seconds")
                    if (
                        not isinstance(age, int)
                        or age < settings.admin_temporary_service_max_age_seconds
                    ):
                        continue
                    if _stop_runtime_service(settings, resource, service):
                        stopped += 1
            return stopped
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_CLEANUP_LOCK_KEY,))


def _managed_runtime_resources(observations: dict[str, Any]) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    for resource in observations.get("managed_resources", []):
        if not isinstance(resource, dict) or resource.get("kind") != "agent-runtime":
            continue
        resource_id = resource.get("resource_id")
        resource_name = resource.get("resource_name")
        if isinstance(resource_id, str) and isinstance(resource_name, str):
            resources.append({"resource_id": resource_id, "resource_name": resource_name})
    return resources


def _runtime_services(settings: Settings, resource: dict[str, str]) -> list[dict[str, Any]]:
    payload = {
        "manager_scope": settings.sandbox_manager_scope,
        "resource_id": resource["resource_id"],
        "resource_name": resource["resource_name"],
        "remove_network": False,
    }
    with httpx.Client(timeout=settings.admin_metrics_request_timeout_seconds) as client:
        response = client.post(
            f"{settings.runtime_provider_url.rstrip('/')}/v1/runtimes/services/list",
            json=payload,
            headers={"Authorization": f"Bearer {settings.admin_runtime_observer_key}"},
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("services"), list):
        raise ValueError("Runtime Provider service scan response must contain a service list")
    return [item for item in body["services"] if isinstance(item, dict)]


def _stop_runtime_service(
    settings: Settings, resource: dict[str, str], service: dict[str, Any]
) -> bool:
    pid = service.get("pid")
    started_ticks = service.get("started_ticks")
    process_group_id = service.get("process_group_id")
    if not isinstance(pid, int) or not isinstance(started_ticks, str):
        return False
    if process_group_id is not None and not isinstance(process_group_id, int):
        return False
    payload = {
        "manager_scope": settings.sandbox_manager_scope,
        "resource_id": resource["resource_id"],
        "resource_name": resource["resource_name"],
        "remove_network": False,
        "pid": pid,
        "started_ticks": started_ticks,
        "process_group_id": process_group_id,
    }
    with httpx.Client(timeout=max(settings.admin_metrics_request_timeout_seconds, 20.0)) as client:
        response = client.post(
            f"{settings.runtime_provider_url.rstrip('/')}/v1/runtimes/services/stop",
            json=payload,
            headers={"Authorization": f"Bearer {settings.admin_runtime_controller_key}"},
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("stopped"), bool):
        raise ValueError("Runtime Provider service stop response must contain a stopped flag")
    return body["stopped"]


def _observations(settings: Settings) -> dict[str, Any]:
    with httpx.Client(timeout=settings.admin_metrics_request_timeout_seconds) as client:
        response = client.get(
            f"{settings.runtime_provider_url.rstrip('/')}/v1/admin/observability",
            headers={"Authorization": f"Bearer {settings.admin_runtime_observer_key}"},
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Runtime Provider observability response must be an object")
    return body


def _samples(observations: dict[str, Any]) -> list[tuple[str, str, str, float]]:
    values: list[tuple[str, str, str, float]] = []
    for service in observations.get("services", []):
        if isinstance(service, dict):
            values.extend(
                _usage_samples("SERVICE", str(service.get("service") or "unknown"), service)
            )
    for resource in observations.get("managed_resources", []):
        if isinstance(resource, dict):
            subject = str(resource.get("resource_id") or resource.get("resource_name") or "unknown")
            values.extend(_usage_samples("RUNTIME", subject, resource))
    return values


def _usage_samples(
    scope: str, subject: str, item: dict[str, Any]
) -> list[tuple[str, str, str, float]]:
    usage = item.get("usage")
    if not isinstance(usage, dict):
        return []
    result: list[tuple[str, str, str, float]] = []
    for metric in ("cpu_usage_percent", "memory_usage_bytes", "storage_usage_bytes"):
        value = usage.get(metric)
        if isinstance(value, int | float):
            result.append((scope, subject, metric, float(value)))
    return result
