"""Short-lived, catalog-only credentials for the built-in Case MCP servers.

A connection test must be able to list tools without holding a coordinator
grant. These credentials never authorize ``tools/call`` and are bound to one
server name, one user and one configured capability version.
"""

import time
from uuid import UUID

from flowweave.modules.cases.application import native_tools
from flowweave.shared.credentials_crypto import decrypt_secret, encrypt_secret

PREFIX = "fwcp."
TTL_SECONDS = 60
CONTRACTS = (native_tools.CONTEXT, native_tools.FLOWS, native_tools.MATERIALS)


def contract_for(server_name: str) -> native_tools.ToolContract | None:
    return next((item for item in CONTRACTS if item.server_name == server_name), None)


def issue(contract: native_tools.ToolContract, owner: str, version_id: str) -> str:
    payload = (
        f"case-catalog|{contract.server_name}|{UUID(owner)}|{UUID(version_id)}|{int(time.time())}"
    )
    return PREFIX + encrypt_secret(payload).decode("ascii")


def identity(token: str, contract: native_tools.ToolContract) -> tuple[str, str]:
    try:
        if not token.startswith(PREFIX) or len(token) > 512:
            raise ValueError("invalid probe")
        purpose, server, owner, version_id, issued = decrypt_secret(
            token[len(PREFIX) :].encode()
        ).split("|")
        age = time.time() - int(issued)
        if (
            purpose != "case-catalog"
            or server != contract.server_name
            or not -5 <= age <= TTL_SECONDS
        ):
            raise ValueError("expired probe")
        return str(UUID(owner)), str(UUID(version_id))
    except Exception:
        raise native_tools.denied() from None
