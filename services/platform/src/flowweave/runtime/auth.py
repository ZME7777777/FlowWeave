from __future__ import annotations

import base64
import hashlib
import hmac


def derive_runtime_session_key(root_key: str, manager_scope: str, resource_name: str) -> str:
    """Derive a stable per-Runtime API key without persisting another secret."""

    if not root_key or not manager_scope or not resource_name:
        raise ValueError("Runtime session key derivation inputs must be non-empty")
    message = f"flowweave-runtime-v1\0{manager_scope}\0{resource_name}".encode()
    digest = hmac.new(root_key.encode(), message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"fwrt_{encoded}"
