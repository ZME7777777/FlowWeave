from __future__ import annotations

import base64
from subprocess import CompletedProcess

import pytest

from flowweave.modules.catalog.application import capability_imports, plugin_sources
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import CapabilityValidateWrite


def test_agent_definition_markdown_is_normalized_from_native_frontmatter() -> None:
    payload = CapabilityValidateWrite(
        capability_type="AGENT_DEFINITION",
        filename="change-reviewer.md",
        content_base64=base64.b64encode(
            b"---\n"
            b"name: change-reviewer\n"
            b"description: Review changes. <example>Review a patch</example>\n"
            b"model: inherit\n"
            b"tools: [terminal]\n"
            b"permission_mode: never_confirm\n"
            b"condenser: none\n"
            b"---\n"
            b"Review the requested change and report verifiable findings.\n"
        ).decode(),
    )

    _, preview = capability_imports._decode_and_validate(payload)

    assert preview["capabilities"] == [
        {
            "capability_key": "change-reviewer",
            "normalized_config": {
                "name": "change-reviewer",
                "description": "Review changes. <example>Review a patch</example>",
                "model": "inherit",
                "tools": ["terminal"],
                "skills": [],
                "system_prompt": "Review the requested change and report verifiable findings.",
                "when_to_use_examples": ["Review a patch"],
                "permission_mode": "never_confirm",
                "max_iteration_per_run": None,
                "max_budget_per_run": None,
                "condenser": {"kind": "NoOpCondenser"},
                "metadata": {},
            },
        }
    ]


def test_hook_import_is_explicitly_retired() -> None:
    payload = CapabilityValidateWrite(
        capability_type="HOOK",
        filename="hook.json",
        content_base64=base64.b64encode(b"{}").decode(),
    )

    with pytest.raises(DomainError, match="Hook") as raised:
        capability_imports._decode_and_validate(payload)

    assert raised.value.status == 410


def test_openhands_marketplace_head_is_resolved_before_catalog_browse(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Resolver:
        def list_marketplace(self, request):
            captured["request"] = request
            return {"commit": request.marketplace_commit}

    monkeypatch.setattr(
        plugin_sources.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, "a" * 40 + "\tHEAD\n", ""),
    )
    monkeypatch.setattr(plugin_sources, "get_plugin_resolver", lambda: Resolver())

    assert plugin_sources.list_openhands_marketplace_catalog() == {"commit": "a" * 40}
    request = captured["request"]
    assert request.marketplace_source == "https://github.com/OpenHands/extensions.git"
    assert request.marketplace_commit == "a" * 40


def test_openhands_marketplace_catalog_binds_the_api_plugin_resolver(client, monkeypatch) -> None:
    """The catalog route must retain the API resolver across ``asyncio.to_thread``."""

    captured: dict[str, object] = {}

    def list_marketplace(request):
        captured["request"] = request
        return {"commit": request.marketplace_commit, "plugins": []}

    monkeypatch.setattr(
        plugin_sources.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, "b" * 40 + "\tHEAD\n", ""),
    )
    monkeypatch.setattr(
        client.app.state.container.plugin_resolver, "list_marketplace", list_marketplace
    )

    response = client.get("/api/v1/plugin-marketplace-catalogs/openhands")

    assert response.status_code == 200, response.text
    assert response.json()["commit"] == "b" * 40
    request = captured["request"]
    assert request.marketplace_source == "https://github.com/OpenHands/extensions.git"
    assert request.marketplace_commit == "b" * 40
