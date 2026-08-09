from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from flowweave.bootstrap.settings import Settings
from flowweave.modules.credentials.application import service
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CredentialConnection, CredentialLease, OAuthSession
from flowweave.shared.settings import settings_context


def test_production_settings_allow_lark_oauth_to_be_disabled() -> None:
    configured = Settings(
        app_env="production",
        credentials_master_key="configured-master-key",
        credential_subject_key="production-user",
        credential_internal_api_key="configured-internal-key",
        lark_oauth_client_id="",
        lark_oauth_client_secret="",
    )

    assert configured.lark_oauth_client_id == ""
    assert configured.lark_oauth_client_secret == ""


def test_oauth_state_pkce_and_tokens_are_not_stored_in_plaintext(
    db_session_factory, settings
) -> None:
    configured = settings.model_copy(
        update={
            "lark_oauth_client_id": "client-id",
            "lark_oauth_client_secret": "client-secret",
        }
    )
    with settings_context(configured), db_session_factory() as db:
        started = service.start_lark_oauth(db, "subject-1", ["docs:read"])
        query = parse_qs(urlparse(started["authorization_url"]).query)
        state = query["state"][0]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0]

        stored = db.scalar(select(OAuthSession))
        assert stored is not None
        assert stored.state_digest == hashlib.sha256(state.encode()).hexdigest()
        assert state.encode() not in stored.encrypted_code_verifier

        exchange = service.consume_oauth_state(db, state)
        with pytest.raises(DomainError, match="OAuth state is invalid or expired"):
            service.consume_oauth_state(db, state)

        service.save_lark_connection(
            db,
            exchange,
            {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "scope": "docs:read",
            },
        )
        connection = db.scalar(select(CredentialConnection))
        assert connection is not None
        assert b"access-secret" not in connection.encrypted_access_token
        assert connection.encrypted_refresh_token is not None
        assert b"refresh-secret" not in connection.encrypted_refresh_token


def test_runtime_lease_is_hashed_limited_and_revocable(db_session_factory, settings) -> None:
    configured = settings.model_copy(update={"credential_lease_max_uses": 1})
    with settings_context(configured), db_session_factory() as db:
        exchange = service.OAuthExchange("subject-1", "verifier", ("docs:read",))
        service.save_lark_connection(
            db,
            exchange,
            {"access_token": "access-secret", "scope": "docs:read"},
        )
        token = service.issue_runtime_lease(
            db,
            subject_key="subject-1",
            provider="lark",
            audience="attempt-1",
            scopes=["docs:read"],
        )
        lease = db.scalar(select(CredentialLease))
        assert lease is not None
        assert lease.token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert token not in lease.token_digest
        assert service.consume_runtime_lease(db, token) == "access-secret"
        with pytest.raises(DomainError, match="Credential lease is invalid or expired"):
            service.consume_runtime_lease(db, token)


def test_internal_lease_endpoint_requires_service_authentication(client) -> None:
    response = client.get("/api/v1/internal/credential-leases/not-a-real-token")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CREDENTIAL_LOOKUP_UNAUTHORIZED"
