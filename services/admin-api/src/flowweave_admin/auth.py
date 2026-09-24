from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from flowweave_admin.database import connect
from flowweave_admin.settings import Settings

_SESSION_COOKIE = "flowweave_session"


async def require_super_admin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    if request.url.path in {"/health", "/metrics"}:
        return await call_next(request)
    settings: Settings = request.app.state.settings
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return JSONResponse(status_code=401, content={"error": {"code": "AUTHENTICATION_REQUIRED"}})
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
        return JSONResponse(status_code=503, content={"error": {"code": "ADMIN_DATA_UNAVAILABLE"}})
    if row is None:
        return JSONResponse(status_code=403, content={"error": {"code": "ADMIN_ACCESS_REQUIRED"}})
    request.state.admin = row
    return await call_next(request)
