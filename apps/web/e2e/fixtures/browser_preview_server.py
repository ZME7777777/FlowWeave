"""Real API/content pipeline with a local Vite server standing in for Docker exec.

No database, production session, Docker socket or running Agent is used.
"""

import base64
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from flowweave.bootstrap import api as bootstrap
from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_workspaces.presentation import router as browser
from flowweave.modules.users.application import service as users
from flowweave.modules.users.application.security import Principal, digest_session_token

PRINCIPAL = Principal("preview-user", "preview-user", "USER", True)
COOKIE = "isolated-browser-preview-test-session"


class Session:
    async def run_sync(self, operation):
        return operation(None)

    async def commit(self):
        pass

    async def rollback(self):
        pass


@asynccontextmanager
async def session_scope():
    yield Session()


async def auxiliary(_container, operation):
    return operation(None)


async def control(_container, operation):
    return operation()


def authorize(_db, workspace_id, **scope):
    assert workspace_id == "schedule-workspace"
    assert scope["binding_id"] == "scheduled-conversation"
    return "preview-runtime", "preview-resource"


def fetch(**payload):
    assert payload["port"] == int(os.environ["PREVIEW_VITE_PORT"])
    path = payload["path"] + ("?" + payload["query"] if payload["query"] else "")
    try:
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{payload['port']}{path}", timeout=10
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return {
            "status": response.status,
            "headers": dict(response.headers.items()),
            "content_base64": base64.b64encode(response.read()).decode(),
        }


settings = Settings(_env_file=None, runtime_adapter="mock")
container = SimpleNamespace(
    settings=settings,
    database=SimpleNamespace(session=session_scope),
    rate_limiter=SimpleNamespace(
        allow=AsyncMock(return_value=SimpleNamespace(allowed=True))
    ),
    metrics=Mock(),
    runtime=Mock(),
    artifact_store=Mock(),
    plugin_resolver=Mock(),
    sandbox=Mock(),
    audit_writer=Mock(),
    conversation_hydration_cache=Mock(),
)
bootstrap.build_container = lambda *_args, **_kwargs: container
users.authenticate = lambda _db, cookie: PRINCIPAL if cookie == COOKIE else None
users.authenticate_session_digest = lambda _db, digest: (
    PRINCIPAL if digest == digest_session_token(COOKIE) else None
)
browser.run_blocking_auxiliary = auxiliary
browser.run_terminal_control = control
browser.workspace.browser_resource_details = authorize
browser.DockerControllerClient = lambda _: SimpleNamespace(browser_fetch=fetch)
api = bootstrap.create_app(settings)
app = FastAPI()


# Simulate an ingress that strips the deployment prefix before reaching the API.
async def ingress(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/flowweave/"):
        scope = {
            **scope,
            "path": scope["path"][10:],
            "raw_path": scope["raw_path"][10:],
        }
        await api(scope, receive, send)
    else:
        await app(scope, receive, send)


@app.get("/host")
def host():
    return HTMLResponse("<h1>Isolated preview host</h1>")


uvicorn.run(
    ingress, host="127.0.0.1", port=int(os.environ["PREVIEW_API_PORT"]), lifespan="off"
)
