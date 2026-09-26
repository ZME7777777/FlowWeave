from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from flowweave_admin.database import connect
from flowweave_admin.settings import Settings

_SESSION_COOKIE = "flowweave_session"


def _error_response(request: Request, *, status: int, code: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", request.headers.get("X-Request-ID"))
    headers = {"X-Request-ID": request_id} if isinstance(request_id, str) else None
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "request_id": request_id}},
        headers=headers,
    )


async def require_super_admin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    if request.url.path in {"/health", "/metrics"}:
        return await call_next(request)
    settings: Settings = request.app.state.settings
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return _error_response(request, status=401, code="AUTHENTICATION_REQUIRED")
    try:
        with connect(settings) as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username
                FROM user_sessions AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.token_digest = %s
                  AND s.expires_at > now()
                  AND u.is_active = true
                  AND u.role = 'SUPER_ADMIN'
                """,
                (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
            ).fetchone()
    except Exception:
        return _error_response(request, status=503, code="ADMIN_DATA_UNAVAILABLE")
    if row is None:
        return _error_response(request, status=403, code="ADMIN_ACCESS_REQUIRED")
    request.state.admin = row
    return await call_next(request)
