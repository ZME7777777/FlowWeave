from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from flowweave.bootstrap.container import Container, build_container
from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_workspaces.presentation.router import router as agent_workspaces_router
from flowweave.modules.catalog.presentation.router import router as catalog_router
from flowweave.modules.conversations.presentation.router import router as conversations_router
from flowweave.modules.environments.presentation.router import router as environments_router
from flowweave.modules.flows.presentation.router import router as flows_router
from flowweave.modules.model_providers.presentation.router import router as providers_router
from flowweave.modules.runs.presentation.router import router as runs_router
from flowweave.runtime.dependencies import bind_runtime, reset_runtime
from flowweave.shared.artifact_store import bind_artifact_store, reset_artifact_store
from flowweave.shared.errors import DomainError
from flowweave.shared.sandbox import bind_sandbox, reset_sandbox
from flowweave.shared.settings import bind_settings, reset_settings


def error_body(code: str, message: str, request_id: str, details: object = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or uuid4())


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings()
    container = build_container(configured, role="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(title="FlowWeave Platform API", version="1.0.0", lifespan=lifespan)
    app.state.container = container

    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        settings_token = bind_settings(container.settings)
        runtime_token = bind_runtime(container.runtime)
        store_token = bind_artifact_store(container.artifact_store)
        sandbox_token = bind_sandbox(container.sandbox)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_sandbox(sandbox_token)
            reset_artifact_store(store_token)
            reset_runtime(runtime_token)
            reset_settings(settings_token)

    async def domain_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, DomainError):
            raise exc
        return JSONResponse(
            status_code=exc.status,
            content=error_body(exc.code, exc.message, _request_id(request), exc.details),
        )

    async def validation_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):
            raise exc
        return JSONResponse(
            status_code=422,
            content=error_body(
                "INVALID_COMMAND",
                "Request validation failed",
                _request_id(request),
                {"errors": jsonable_encoder(exc.errors())},
            ),
        )

    async def integrity_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, IntegrityError):
            raise exc
        diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
        constraint = getattr(diagnostic, "constraint_name", None)
        if constraint == "flow_definitions_name_key":
            code = "FLOW_NAME_CONFLICT"
            message = "流程名称已存在，请使用其他名称。"
        elif constraint == "uq_asset_active_directory_name":
            code = "NODE_ASSET_NAME_CONFLICT"
            message = "当前目录已存在同名节点资产，请使用其他名称。"
        else:
            code = "DATA_CONFLICT"
            message = "提交的数据与现有记录冲突，请检查是否存在重名或重复关联。"
        return JSONResponse(
            status_code=409,
            content=error_body(
                code,
                message,
                _request_id(request),
            ),
        )

    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    async def readiness(request: Request) -> dict[str, str]:
        active: Container = request.app.state.container
        await active.database.ping()
        return {"status": "ready"}

    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.middleware("http")(request_context)
    app.add_exception_handler(DomainError, domain_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(IntegrityError, integrity_error)
    app.add_api_route("/health/live", liveness, methods=["GET"])
    app.add_api_route("/health/ready", readiness, methods=["GET"])
    app.add_api_route("/health", health, methods=["GET"])

    for router in (
        agent_workspaces_router,
        catalog_router,
        environments_router,
        providers_router,
        flows_router,
        runs_router,
        conversations_router,
    ):
        app.include_router(router, prefix="/api/v1")
    return app
