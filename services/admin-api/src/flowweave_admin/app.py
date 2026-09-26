from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from flowweave_admin.alerts import apply_lifecycle, realtime_alerts
from flowweave_admin.auth import require_super_admin
from flowweave_admin.control import (
    AdminControlError,
    AlertLifecycleCommand,
    RuntimeReplacementCommand,
    request_runtime_replacement,
    update_alert_lifecycle,
)
from flowweave_admin.database import connect
from flowweave_admin.observability import (
    admin_operations,
    alert_states,
    background_task_summary,
    background_tasks,
    conversations,
    enrich_runtime_operation_status,
    metric_history,
    overview,
    runtime_detail,
    runtime_observations,
    runtime_operations,
    runtimes,
    service_metrics,
)
from flowweave_admin.settings import Settings


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        yield

    app = FastAPI(title="FlowWeave Admin API", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(require_super_admin)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse("flowweave_admin_up 1\n", media_type="text/plain; version=0.0.4")

    @app.get("/v1/admin/me")
    async def me(request: Request) -> dict[str, Any]:
        return {"user": dict(request.state.admin)}

    @app.get("/v1/admin/overview")
    async def admin_overview(request: Request) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            database = overview(connection)
        services, observations = await asyncio.gather(
            service_metrics(active_settings), runtime_observations(active_settings)
        )
        return {
            "database": database,
            "services": services,
            "container_observability": observations,
        }

    @app.get("/v1/admin/alerts")
    async def admin_alerts(request: Request) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            database = overview(connection)
            runtime_entries = runtimes(connection, limit=500)
        services, observations = await asyncio.gather(
            service_metrics(active_settings), runtime_observations(active_settings)
        )
        usage_by_resource = {
            str(item.get("resource_id")): item.get("usage")
            for item in observations.get("managed_resources", [])
            if isinstance(item, dict)
        }
        for runtime in runtime_entries:
            sandbox_id = runtime.get("managed_sandbox_id")
            runtime["usage"] = usage_by_resource.get(str(sandbox_id)) if sandbox_id else None
        alerts = realtime_alerts(
            active_settings,
            database=database,
            services=services,
            observations=observations,
            runtimes=runtime_entries,
        )
        with connect(active_settings) as connection:
            states = alert_states(connection, keys=[str(item["key"]) for item in alerts])
        apply_lifecycle(alerts, states)
        return {
            "items": alerts,
            "summary": {
                "critical": sum(item["severity"] == "CRITICAL" for item in alerts),
                "warning": sum(item["severity"] == "WARNING" for item in alerts),
            },
        }

    @app.post("/v1/admin/alerts/lifecycle", response_model=None)
    async def admin_alert_lifecycle(
        payload: AlertLifecycleCommand, request: Request
    ) -> dict[str, Any] | JSONResponse:
        active_settings: Settings = request.app.state.settings
        actor = request.state.admin
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        try:
            return await update_alert_lifecycle(
                active_settings,
                payload,
                actor_user_id=str(actor["id"]),
                actor_username=str(actor["username"]),
                request_id=request_id,
            )
        except AdminControlError as exc:
            return JSONResponse(
                status_code=exc.status,
                content={"error": {"code": exc.code, "message": str(exc)}},
            )

    @app.get("/v1/admin/metric-history", response_model=None)
    async def admin_metric_history(
        request: Request,
        scope: str,
        subject: str,
        metric: str,
        hours: int = 24,
    ) -> dict[str, Any] | JSONResponse:
        if scope not in {"SERVICE", "RUNTIME"}:
            return JSONResponse(
                status_code=400, content={"error": {"message": "Invalid metric scope"}}
            )
        if metric not in {"cpu_usage_percent", "memory_usage_bytes", "storage_usage_bytes"}:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid metric"}})
        if not subject or len(subject) > 200 or not 1 <= hours <= 168:
            return JSONResponse(
                status_code=400, content={"error": {"message": "Invalid history query"}}
            )
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            return {
                "scope": scope,
                "subject": subject,
                "metric": metric,
                "hours": hours,
                "items": metric_history(
                    connection, scope=scope, subject=subject, metric=metric, hours=hours
                ),
            }

    @app.get("/v1/admin/runtimes")
    async def admin_runtimes(request: Request, limit: int = 100) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        bounded_limit = min(max(limit, 1), 500)
        with connect(active_settings) as connection:
            entries = runtimes(connection, limit=bounded_limit)
        observations = await runtime_observations(active_settings)
        usage_by_resource = {
            str(item.get("resource_id")): item.get("usage")
            for item in observations.get("managed_resources", [])
            if isinstance(item, dict)
        }
        for entry in entries:
            sandbox_id = entry.get("managed_sandbox_id")
            entry["usage"] = usage_by_resource.get(str(sandbox_id)) if sandbox_id else None
        return {
            "items": entries,
            "container_observability_available": observations.get("available", False),
        }

    @app.get("/v1/admin/runtimes/{runtime_session_id}", response_model=None)
    async def admin_runtime_detail(
        runtime_session_id: str, request: Request
    ) -> dict[str, Any] | JSONResponse:
        if len(runtime_session_id) != 36:
            return JSONResponse(
                status_code=400, content={"error": {"message": "Invalid Runtime Session"}}
            )
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            detail = runtime_detail(connection, runtime_session_id=runtime_session_id)
        if detail is None:
            return JSONResponse(
                status_code=404, content={"error": {"message": "Runtime was not found"}}
            )
        observations = await runtime_observations(active_settings)
        sandbox_id = detail["runtime"].get("managed_sandbox_id")
        usage_by_resource = {
            str(item.get("resource_id")): item.get("usage")
            for item in observations.get("managed_resources", [])
            if isinstance(item, dict)
        }
        detail["runtime"]["usage"] = (
            usage_by_resource.get(str(sandbox_id)) if sandbox_id is not None else None
        )
        detail["container_observability_available"] = observations.get("available", False)
        return detail

    @app.get("/v1/admin/background-tasks")
    async def admin_background_tasks(request: Request, limit: int = 500) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        bounded_limit = min(max(limit, 1), 500)
        with connect(active_settings) as connection:
            return {
                "summary": background_task_summary(
                    connection, retention_days=active_settings.task_terminal_retention_days
                ),
                "items": background_tasks(connection, limit=bounded_limit),
            }

    @app.get("/v1/admin/conversations")
    async def admin_conversations(request: Request, limit: int = 100) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            return {"items": conversations(connection, limit=min(max(limit, 1), 500))}

    @app.get("/v1/admin/runtime-operations")
    async def admin_runtime_operations(request: Request, limit: int = 100) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            entries = runtime_operations(connection, limit=min(max(limit, 1), 500))
        return {"items": enrich_runtime_operation_status(entries)}

    @app.get("/v1/admin/operations", response_model=None)
    async def all_admin_operations(
        request: Request,
        action: str | None = None,
        actor: str | None = None,
        since_hours: int = 168,
        limit: int = 200,
    ) -> dict[str, Any] | JSONResponse:
        allowed_actions = {"REPLACE_RUNTIME", "ACKNOWLEDGE", "SILENCE"}
        if action is not None and action not in allowed_actions:
            return JSONResponse(
                status_code=400, content={"error": {"message": "Invalid operation action"}}
            )
        if (actor is not None and len(actor) > 80) or not 1 <= since_hours <= 8_760:
            return JSONResponse(
                status_code=400, content={"error": {"message": "Invalid operation query"}}
            )
        active_settings: Settings = request.app.state.settings
        with connect(active_settings) as connection:
            return {
                "items": admin_operations(
                    connection,
                    action=action,
                    actor=actor,
                    since_hours=since_hours,
                    limit=min(max(limit, 1), 500),
                )
            }

    @app.post("/v1/admin/runtime-replacements", status_code=202, response_model=None)
    async def admin_runtime_replacement(
        payload: RuntimeReplacementCommand, request: Request
    ) -> dict[str, Any] | JSONResponse:
        active_settings: Settings = request.app.state.settings
        actor = request.state.admin
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        try:
            return await request_runtime_replacement(
                active_settings,
                payload,
                actor_user_id=str(actor["id"]),
                actor_username=str(actor["username"]),
                request_id=request_id,
            )
        except AdminControlError as exc:
            return JSONResponse(
                status_code=exc.status,
                content={"error": {"code": exc.code, "message": str(exc)}},
            )

    return app
