from __future__ import annotations

from typing import Any

from flowweave_admin.settings import Settings


def realtime_alerts(
    settings: Settings,
    *,
    database: dict[str, Any],
    services: dict[str, Any],
    observations: dict[str, Any],
    runtimes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for service_name, service in services.items():
        health = str(service.get("health") or "UNREACHABLE")
        if health != "UP":
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    source="SERVICE",
                    key=f"service:{service_name}",
                    title=f"服务 {service_name} 不可用",
                    detail=f"健康检查状态为 {health}",
                )
            )

    for container in observations.get("services", []):
        if not isinstance(container, dict):
            continue
        service_name = str(container.get("service") or "unknown")
        state = str(container.get("state") or "UNKNOWN")
        if state != "RUNNING":
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    source="CONTAINER",
                    key=f"container:{container.get('container_id')}",
                    title=f"容器 {service_name} 未运行",
                    detail=f"容器状态为 {state}",
                )
            )
            continue
        usage = container.get("usage")
        if isinstance(usage, dict):
            _append_usage_alerts(
                alerts,
                settings,
                source="CONTAINER",
                key=f"container:{container.get('container_id')}",
                title=service_name,
                usage=usage,
            )

    for runtime in runtimes:
        runtime_id = str(runtime.get("runtime_session_id") or "unknown")
        status = str(runtime.get("status") or "UNKNOWN")
        if status in {"FAILED", "STOPPED"}:
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    source="RUNTIME",
                    key=f"runtime:{runtime_id}:status",
                    title=f"Runtime {runtime_id[:8]} 处于 {status}",
                    detail=str(
                        runtime.get("failure_summary")
                        or runtime.get("last_error_detail")
                        or "需要人工处理"
                    ),
                )
            )
        elif status in {"DEGRADED", "RECONNECTING"}:
            alerts.append(
                _alert(
                    severity="WARNING",
                    source="RUNTIME",
                    key=f"runtime:{runtime_id}:status",
                    title=f"Runtime {runtime_id[:8]} 处于 {status}",
                    detail=str(runtime.get("failure_summary") or "替换或恢复正在进行"),
                )
            )
        usage = runtime.get("usage")
        if isinstance(usage, dict):
            _append_usage_alerts(
                alerts,
                settings,
                source="RUNTIME",
                key=f"runtime:{runtime_id}",
                title=f"Runtime {runtime_id[:8]}",
                usage=usage,
            )

    active_connections = sum(
        int(row.get("count") or 0)
        for row in database.get("database_connections", [])
        if row.get("state") != "idle"
    )
    if active_connections >= settings.admin_alert_database_active_connections_threshold:
        alerts.append(
            _alert(
                severity="WARNING",
                source="DATABASE",
                key="database:active-connections",
                title="数据库活跃连接偏高",
                detail=(
                    f"当前活跃连接 {active_connections}，阈值 "
                    f"{settings.admin_alert_database_active_connections_threshold}"
                ),
            )
        )

    for task in database.get("tasks", []):
        state = str(task.get("state") or "UNKNOWN")
        count = int(task.get("count") or 0)
        if state in {"PENDING", "RETRY"} and count >= settings.admin_alert_pending_task_threshold:
            alerts.append(
                _alert(
                    severity="WARNING",
                    source="BACKGROUND_TASK",
                    key=f"task:{state}",
                    title=f"后台任务 {state} 积压",
                    detail=(
                        f"当前 {count} 个任务，阈值 "
                        f"{settings.admin_alert_pending_task_threshold}"
                    ),
                )
            )
    return sorted(alerts, key=lambda item: (item["severity"] != "CRITICAL", item["key"]))


def apply_lifecycle(
    alerts: list[dict[str, Any]], states: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key = {str(state["alert_key"]): state for state in states}
    for alert in alerts:
        state = by_key.get(str(alert["key"]))
        if state is not None:
            alert["lifecycle"] = state
    return alerts


def _append_usage_alerts(
    alerts: list[dict[str, Any]],
    settings: Settings,
    *,
    source: str,
    key: str,
    title: str,
    usage: dict[str, Any],
) -> None:
    cpu = _number(usage.get("cpu_usage_percent"))
    if cpu is not None and cpu >= settings.admin_alert_cpu_percent_threshold:
        alerts.append(
            _alert(
                severity="WARNING",
                source=source,
                key=f"{key}:cpu",
                title=f"{title} CPU 使用率偏高",
                detail=f"当前 {cpu:.1f}%，阈值 {settings.admin_alert_cpu_percent_threshold:.1f}%",
            )
        )
    memory_usage = _number(usage.get("memory_usage_bytes"))
    memory_limit = _memory_limit_bytes(usage.get("memory_limit"))
    if memory_usage is not None and memory_limit and memory_limit > 0:
        percent = memory_usage / memory_limit * 100
        if percent >= settings.admin_alert_memory_percent_threshold:
            alerts.append(
                _alert(
                    severity="WARNING",
                    source=source,
                    key=f"{key}:memory",
                    title=f"{title} 内存使用率偏高",
                    detail=(
                        f"当前 {percent:.1f}%，阈值 "
                        f"{settings.admin_alert_memory_percent_threshold:.1f}%"
                    ),
                )
            )


def _alert(*, severity: str, source: str, key: str, title: str, detail: str) -> dict[str, str]:
    return {
        "severity": severity,
        "source": source,
        "key": key,
        "title": title,
        "detail": detail,
    }


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _memory_limit_bytes(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    numeric, separator, unit = value.strip().partition(" ")
    if not separator:
        return None
    try:
        scale = {"B": 1, "KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000}[unit.upper()]
        return float(numeric) * scale
    except (KeyError, ValueError):
        return None
