import pytest

from flowweave.modules.model_providers.domain.protocols import (
    anthropic_base_url,
    infer_model_protocol,
    openai_base_url,
    protocol_auth_headers,
    protocol_base_url,
    resolve_model_protocol,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-sonnet-5-5", "ANTHROPIC"),
        ("hst/claude-opus-5-5", "ANTHROPIC"),
        ("gpt-5.5", "RESPONSES"),
        ("gpt-5-codex", "RESPONSES"),
        ("o3", "RESPONSES"),
        ("gpt-4.1", "CHAT_COMPLETIONS"),
        ("gpt-4o", "CHAT_COMPLETIONS"),
        ("gpt-oss-120b", "CHAT_COMPLETIONS"),
        ("deepseek-chat", "CHAT_COMPLETIONS"),
    ],
)
def test_protocol_is_inferred_from_the_model_family(name, expected):
    assert infer_model_protocol(name) == expected


def test_explicit_override_wins_over_inference():
    assert resolve_model_protocol("gpt-5.5", "CHAT_COMPLETIONS") == "CHAT_COMPLETIONS"
    assert resolve_model_protocol("gpt-5.5", None) == "RESPONSES"


def test_one_origin_derives_endpoint_per_protocol():
    origin = "https://gateway.example.test"
    assert openai_base_url(origin) == f"{origin}/v1"
    assert openai_base_url(f"{origin}/v1/") == f"{origin}/v1"
    assert openai_base_url(f"{origin}/custom/v1") == f"{origin}/custom/v1"
    assert anthropic_base_url(f"{origin}/v1") == origin
    assert protocol_base_url(origin, "ANTHROPIC") == origin
    assert protocol_base_url(origin, "RESPONSES") == f"{origin}/v1"


def test_authentication_header_follows_the_protocol():
    assert protocol_auth_headers("k", "ANTHROPIC")["x-api-key"] == "k"
    assert "Authorization" not in protocol_auth_headers("k", "ANTHROPIC")
    assert protocol_auth_headers("k", "RESPONSES") == {"Authorization": "Bearer k"}
    assert protocol_auth_headers("k", "CHAT_COMPLETIONS") == {"Authorization": "Bearer k"}
