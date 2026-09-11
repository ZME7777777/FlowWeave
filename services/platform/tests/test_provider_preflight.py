from __future__ import annotations

import pytest

from flowweave.modules.model_providers.application.service import provider_has_runtime_credentials
from flowweave.shared.models import ModelProvider


@pytest.fixture(autouse=True)
def database():
    """Provider credential predicates are pure and require no Testcontainer."""

    yield


@pytest.mark.parametrize(
    ("auth_type", "api_key", "oauth_access", "oauth_refresh", "expected"),
    [
        ("API_KEY", b"encrypted-api-key", None, None, True),
        ("API_KEY", None, None, None, False),
        ("CODEX_OAUTH", None, b"encrypted-access", b"encrypted-refresh", True),
        ("CODEX_OAUTH", None, b"encrypted-access", None, False),
        ("CODEX_OAUTH", None, None, b"encrypted-refresh", False),
    ],
)
def test_runtime_credential_preflight_requires_refreshable_credentials(
    auth_type: str,
    api_key: bytes | None,
    oauth_access: bytes | None,
    oauth_refresh: bytes | None,
    expected: bool,
) -> None:
    provider = ModelProvider(
        name="credential-preflight",
        base_url="https://models.example.test/v1",
        auth_type=auth_type,
        encrypted_api_key=api_key,
        encrypted_oauth_access_token=oauth_access,
        encrypted_oauth_refresh_token=oauth_refresh,
    )

    assert provider_has_runtime_credentials(provider) is expected
