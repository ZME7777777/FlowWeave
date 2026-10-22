"""Short-lived, session-bound authorization for opaque-origin preview resources."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from time import time

from cryptography.fernet import InvalidToken

from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret
from flowweave.shared.errors import DomainError

PREVIEW_TTL_SECONDS = 900
PREVIEW_PATH = re.compile(
    r"^/api/v1/agent-workspaces/(?P<workspace>[^/]+)/browser/"
    r"(?P<scheme>https?)/(?P<port>[0-9]+)/_preview/(?P<token>fwpreview_[A-Za-z0-9_=-]+)/"
)


@dataclass(frozen=True)
class BrowserPreview:
    user_id: str
    session_digest: str
    workspace_id: str
    scheme: str
    port: int
    binding_id: str | None
    work_directory_id: str | None
    deployment_base_path: str
    expires_at: int

    def encode(self) -> str:
        return "fwpreview_" + encrypt_secret(json.dumps(asdict(self))).decode()


def read_preview(path: str) -> BrowserPreview | None:
    """Only the exact read-only browser route may authenticate with this grant."""

    match = PREVIEW_PATH.match(path)
    if match is None:
        return None
    try:
        payload = json.loads(decrypt_secret(match["token"][10:].encode()))
        preview = BrowserPreview(**payload)
        if (
            preview.expires_at <= time()
            or preview.workspace_id != match["workspace"]
            or preview.scheme != match["scheme"]
            or preview.port != int(match["port"])
        ):
            raise ValueError("Preview scope mismatch")
        return preview
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise DomainError(
            "AGENT_BROWSER_PREVIEW_EXPIRED", "预览已过期，请刷新内置浏览器", 401
        ) from None


def deployment_path(value: str) -> str:
    value = value.rstrip("/")
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", value):
        raise DomainError("AGENT_BROWSER_PATH_INVALID", "浏览器部署路径无效", 422)
    return value
