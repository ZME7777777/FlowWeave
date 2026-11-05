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


def _payload(name: str):
    import base64
    import json

    from flowweave.shared.schemas import CapabilityValidateWrite

    config = {
        "mcpServers": {
            name: {
                "url": "http://platform.example.test/api/v1/case-tools-mcp",
                "transport": "streamable-http",
                "headers": {"Authorization": PLATFORM_MCP_SECRET_REFERENCES[name]},
            }
        }
    }
    return CapabilityValidateWrite(
        capability_type="MCP",
        filename=name + ".json",
        content_base64=base64.b64encode(json.dumps(config).encode()).decode(),
    )


@pytest.mark.parametrize("name", [n for n in PLATFORM_MCP_SECRET_REFERENCES if "case" in n])
def test_case_mcp_import_requires_the_governed_path(name, monkeypatch):
    from types import SimpleNamespace

    from flowweave.modules.catalog.application import capability_imports

    monkeypatch.setattr(
        capability_imports,
        "get_settings",
        lambda: SimpleNamespace(capability_import_ttl_seconds=900),
    )
    with pytest.raises(DomainError) as denied:
        capability_imports.prepare_validation(_payload(name))
    assert denied.value.code == "CAPABILITY_PLATFORM_MANAGED"
    prepared = capability_imports.prepare_validation(_payload(name), allow_platform_managed=True)
    assert prepared.preview["capabilities"][0]["capability_key"] == name
