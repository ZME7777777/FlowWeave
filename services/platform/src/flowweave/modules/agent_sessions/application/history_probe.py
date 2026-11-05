"""Short-lived, catalog-only credentials. These never authorize history reads."""

import time
from uuid import UUID

from flowweave.modules.agent_sessions.application import history_access
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret

PREFIX = "fwhp."
TTL_SECONDS = 60


def issue(owner: str, version_id: str) -> str:
    payload = f"history-catalog|{UUID(owner)}|{UUID(version_id)}|{int(time.time())}"
    return PREFIX + encrypt_secret(payload).decode("ascii")


def identity(token: str) -> tuple[str, str]:
    try:
        if not token.startswith(PREFIX) or len(token) > 512:
            raise ValueError("invalid probe")
        purpose, owner, version_id, issued = decrypt_secret(token[len(PREFIX) :].encode()).split(
            "|"
        )
        age = time.time() - int(issued)
        if purpose != "history-catalog" or not -5 <= age <= TTL_SECONDS:
            raise ValueError("expired probe")
        return str(UUID(owner)), str(UUID(version_id))
    except Exception:
        raise history_access.denied() from None
