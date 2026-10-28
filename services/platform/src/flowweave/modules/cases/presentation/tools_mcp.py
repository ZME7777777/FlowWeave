"""Native read-only Case MCP endpoint, independent of platform login cookies."""

import json
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from flowweave.modules.cases.application import context_tools, native_tools
from flowweave.modules.users.application.security import Principal
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.shared.errors import DomainError
from flowweave.shared.http import get_container, run_blocking_history

router = APIRouter()
MCP_PATH = native_tools.MCP_PATH


def result(identity: str | int, value: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": identity, "result": value}, headers={"Cache-Control": "no-store"}
    )


def error(identity: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


@router.post(MCP_PATH)
async def case_tools_mcp(request: Request) -> Response:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 256:
        raise native_tools.denied()
    token = header[7:]
    owner, _ = native_tools.token_identity(token)
    container = get_container(request)

    def authorize(db: Session) -> Principal | None:
        native_tools.authenticate(db, token)
        return agent_session_principal(db, owner)

    request.state.audit_principal = await run_blocking_history(container, authorize)
    decision = await container.rate_limiter.allow(
        "case_tools_mcp", owner, limit=60, window_seconds=60
    )
    if not decision.allowed:
        raise DomainError("CASE_TOOL_RATE_LIMIT", "场景查询过于频繁，请稍后重试", 429)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16_384:
            return error(None, -32600, "Request too large")
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return error(None, -32700, "Parse error")
    if not isinstance(message, dict):
        return error(None, -32600, "Invalid Request")
    message = cast(dict[str, Any], message)
    if message.get("jsonrpc") != "2.0":
        return error(None, -32600, "Invalid Request")
    identity, method = message.get("id"), message.get("method")
    if "id" not in message and isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=202)
    if not isinstance(identity, str | int) or isinstance(identity, bool):
        return error(None, -32600, "Request ID required")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error(identity, -32602, "Invalid params")
    params = cast(dict[str, Any], params)
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (
            requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-03-26"
        )
        return result(
            identity,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": native_tools.SERVER_NAME, "version": "1.0.0"},
            },
        )
    if method == "ping":
        return result(identity, {})
    if method == "tools/list":
        return result(identity, {"tools": [context_tools.TOOL]})
    if method != "tools/call":
        return error(identity, -32601, "Method not found")
    if params.get("name") != context_tools.TOOL["name"]:
        return error(identity, -32602, "Unknown tool")
    try:
        args = context_tools.ContextArguments.model_validate(params.get("arguments", {}))
    except ValidationError:
        return error(identity, -32602, "Invalid tool arguments")
    projection = await run_blocking_history(
        container, lambda db: context_tools.read_context(db, token, args)
    )
    return result(
        identity,
        {
            "content": [{"type": "text", "text": json.dumps(projection, ensure_ascii=False)}],
            "isError": False,
        },
    )
