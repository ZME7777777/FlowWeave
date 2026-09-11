from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_sessions.application import runtime_config
from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.runtime.base import RuntimeProvider
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import AgentPresetWrite


@pytest.fixture(autouse=True)
def database():
    """These frozen-policy checks are deliberately independent of PostgreSQL."""

    yield


def _binding(fallback_models_json: object) -> AgentConversationBinding:
    return AgentConversationBinding(
        workspace_id=None,
        runtime_session_id="runtime",
        host_kind="FLOW_NODE",
        host_id="host",
        conversation_scope_id="scope",
        flow_run_id="run",
        node_run_id="node-run",
        node_attempt_id="attempt",
        owner_user_id="owner",
        model_provider_id="primary",
        model_name="primary-model",
        reasoning_effort=None,
        fallback_models_json=fallback_models_json,
        openhands_conversation_id="conversation",
        create_idempotency_key="key",
    )


def test_agent_preset_fallback_schema_is_explicit_ordered_and_bounded() -> None:
    preset = AgentPresetWrite(
        model_provider_id="primary",
        model_name="primary-model",
        fallback_models=[
            {"model_provider_id": "secondary", "model_name": "secondary-model"}
        ],
    )
    assert [item.model_name for item in preset.fallback_models] == ["secondary-model"]

    with pytest.raises(ValidationError, match="fallback models must be unique"):
        AgentPresetWrite(
            fallback_models=[
                {"model_provider_id": "secondary", "model_name": "model"},
                {"model_provider_id": "secondary", "model_name": "model"},
            ]
        )
    with pytest.raises(ValidationError, match="must differ from the primary"):
        AgentPresetWrite(
            model_provider_id="primary",
            model_name="model",
            fallback_models=[{"model_provider_id": "primary", "model_name": "model"}],
        )
    with pytest.raises(ValidationError):
        AgentPresetWrite(
            fallback_models=[
                {"model_provider_id": f"provider-{index}", "model_name": "model"}
                for index in range(4)
            ]
        )


def test_resolve_session_config_normalizes_and_freezes_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_config, "default_workspace", lambda _db: None)
    monkeypatch.setattr(runtime_config, "has_connected_default_model", lambda _db, _id: True)
    monkeypatch.setattr(
        runtime_config,
        "resolve_runtime_selection",
        lambda _db, node, model, effort: (model, effort),
    )

    frozen = runtime_config.resolve_session_config(
        object(),
        model_provider_id="primary",
        model_name="primary-model",
        fallback_models=(
            {"model_provider_id": "secondary", "model_name": "second"},
            {"model_provider_id": "third", "model_name": "third", "reasoning_effort": "high"},
        ),
        capability_version_ids=(),
    )

    assert [item.as_dict() for item in frozen.fallback_models] == [
        {"model_provider_id": "secondary", "model_name": "second", "reasoning_effort": None},
        {"model_provider_id": "third", "model_name": "third", "reasoning_effort": "high"},
    ]


@pytest.mark.parametrize(
    "policy",
    (
        [{"model_provider_id": "secondary"}],
        ["not-an-object"],
        [{"model_provider_id": "primary", "model_name": "primary-model"}],
        [
            {"model_provider_id": "secondary", "model_name": "model"},
            {"model_provider_id": "secondary", "model_name": "model"},
        ],
    ),
)
def test_binding_fallback_policy_fails_closed_when_tampered(policy: object) -> None:
    db = SimpleNamespace(scalars=lambda _query: [])
    with pytest.raises(DomainError, match="Frozen fallback policy is invalid") as raised:
        runtime_config.config_from_binding(db, _binding(policy))
    assert raised.value.code == "AGENT_FALLBACK_POLICY_INVALID"


def test_provider_for_config_resolves_fallbacks_only_from_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_runtime_provider(
        _db: object, node: dict[str, object], **kwargs: object
    ) -> RuntimeProvider:
        executor = node["asset"]["executor"]
        provider_id = executor["model_provider_id"]
        assert isinstance(provider_id, str)
        model_name = kwargs["model_name"]
        assert isinstance(model_name, str)
        calls.append((provider_id, model_name))
        return RuntimeProvider(provider_id, "https://models.test/v1", model_name, "secret")

    monkeypatch.setattr(runtime_config, "runtime_provider", fake_runtime_provider)
    config = runtime_config.FrozenSessionConfig(
        None,
        "primary",
        "primary-model",
        None,
        (),
        (runtime_config.FrozenModelFallback("fallback", "fallback-model", None),),
    )

    provider = runtime_config.provider_for_config(object(), config)

    assert provider is not None
    assert calls == [("primary", "primary-model"), ("fallback", "fallback-model")]
    assert [item.model for item in provider.fallback_providers] == ["fallback-model"]


def test_openhands_payload_uses_only_formal_profile_based_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = OpenHandsRuntime(
        Settings(
            runtime_adapter="openhands",
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            sandbox_manager_scope="fallback-test",
        )
    )
    fallback = RuntimeProvider(
        "fallback", "https://fallback.test/v1", "fallback-model", "fallback-secret"
    )
    primary = RuntimeProvider(
        "primary",
        "https://primary.test/v1",
        "primary-model",
        "primary-secret",
        fallback_providers=(fallback,),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        calls.append((method, path, payload))
        return {"name": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(runtime, "_request", fake_request)
    names = runtime._configure_fallback_profiles(  # pyright: ignore[reportPrivateUsage]
        provider=primary,
        base_url="http://runtime:8000",
        session_api_key="runtime-session-key",
    )

    assert names == runtime._fallback_profile_names((fallback,))  # pyright: ignore[reportPrivateUsage]
    assert names[0].startswith("flowweave-fb-")
    assert calls == [
        (
            "POST",
            f"/api/profiles/{names[0]}",
            {
                "llm": runtime._llm_payload(fallback),  # pyright: ignore[reportPrivateUsage]
                "include_secrets": True,
            },
        )
    ]
    assert runtime._llm_payload(primary)["api_key"] == "primary-secret"  # pyright: ignore[reportPrivateUsage]
    assert runtime._llm_payload(primary, fallback_profile_names=names)[  # pyright: ignore[reportPrivateUsage]
        "fallback_strategy"
    ] == {"fallback_llms": list(names)}
