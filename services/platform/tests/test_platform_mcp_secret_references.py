import pytest

from flowweave.modules.catalog.application.capability_imports import _reject_sensitive
from flowweave.modules.catalog.application.platform_capabilities import (
    PLATFORM_MCP_SECRET_REFERENCES,
)
from flowweave.shared.errors import DomainError


def _config(name: str, value: str) -> dict:
    return {"mcpServers": {name: {"headers": {"Authorization": value}}}}


@pytest.mark.parametrize(("name", "reference"), PLATFORM_MCP_SECRET_REFERENCES.items())
def test_platform_mcp_accepts_only_its_exact_secret_reference(name, reference):
    _reject_sensitive(_config(name, reference), mcp_secret_references=True)
    with pytest.raises(DomainError):
        _reject_sensitive(_config(name, "Bearer plaintext"), mcp_secret_references=True)
    with pytest.raises(DomainError):
        _reject_sensitive(_config(name, reference), mcp_secret_references=False)


def test_platform_mcp_reference_is_not_portable_between_servers():
    context = PLATFORM_MCP_SECRET_REFERENCES["flowweave-case-context"]
    with pytest.raises(DomainError):
        _reject_sensitive(_config("flowweave-case-flows", context), mcp_secret_references=True)
    with pytest.raises(DomainError):
        _reject_sensitive(_config("user-server", context), mcp_secret_references=True)
