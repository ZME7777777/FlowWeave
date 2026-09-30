from __future__ import annotations

import ssl
from socket import socket
from threading import Thread
from uuid import UUID

import pytest
from ldap3 import MOCK_SYNC, OFFLINE_SLAPD_2_4, Connection, Server

from flowweave.bootstrap.settings import Settings
from flowweave.modules.users.application import ldap
from flowweave.shared.errors import DomainError


def settings(**overrides: object) -> Settings:
    values = {
        "auth_provider": "ldap",
        "ldap_url": "ldap://directory.example.test:389",
        "ldap_base_dn": "dc=example,dc=test",
        "ldap_search_bind_dn": "cn=reader,dc=example,dc=test",
        "ldap_search_bind_password": "test-reader-password",
        "ldap_start_tls": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def install_directory(monkeypatch: pytest.MonkeyPatch, count: int = 1) -> list[Connection]:
    connections: list[Connection] = []

    def server(host: str, **kwargs: object) -> Server:
        kwargs["get_info"] = OFFLINE_SLAPD_2_4
        return Server(host, **kwargs)

    def connect(server: object, **kwargs: object) -> Connection:
        connection = Connection(server, client_strategy=MOCK_SYNC, check_names=False, **kwargs)
        connection.strategy.add_entry(
            "cn=reader,dc=example,dc=test", {"userPassword": "test-reader-password"}
        )
        for dn, name in (
            ("ou=People,dc=example,dc=test", "People"),
            ("ou=Engineering,dc=example,dc=test", "Engineering"),
            ("ou=Platform,ou=Engineering,dc=example,dc=test", "Platform"),
            ("ou=Research,dc=example,dc=test", "Research"),
        ):
            connection.strategy.add_entry(
                dn,
                {"objectClass": ["top", "organizationalUnit"], "ou": name},
            )
        for index in range(count):
            username = f"employee-{index:04d}"
            organization_dn = (
                "ou=Platform,ou=Engineering,dc=example,dc=test"
                if index == 0
                else "ou=People,dc=example,dc=test"
            )
            connection.strategy.add_entry(
                f"uid={username},{organization_dn}",
                {
                    "objectClass": "inetOrgPerson",
                    "uid": username,
                    "entryUUID": str(UUID(int=index + 1)),
                    "cn": username,
                    "mail": f"{username}@example.test",
                    "userPassword": "test-user-password",
                },
            )
        connections.append(connection)
        return connection

    monkeypatch.setattr(ldap, "Connection", connect)
    monkeypatch.setattr(ldap, "Server", server)
    return connections


def test_plaintext_requires_explicit_configuration_and_keeps_tls_default() -> None:
    assert settings().ldap_start_tls is False
    assert settings(ldap_start_tls=True).ldap_start_tls is True
    assert Settings(_env_file=None).ldap_start_tls is True
    with pytest.raises(ValueError, match="search bind credentials"):
        settings(ldap_search_bind_password="")


def test_native_open_none_is_success_and_bind_checks_user_password(monkeypatch) -> None:
    connections = install_directory(monkeypatch)
    directory = ldap.LdapDirectory(settings())
    assert directory.authenticate("employee-0000", "test-user-password").username == "employee-0000"
    assert all(connection.closed for connection in connections)
    with pytest.raises(DomainError) as rejected:
        directory.authenticate("employee-0000", "incorrect-password")
    assert rejected.value.code == "AUTHENTICATION_FAILED"
    assert all(connection.closed for connection in connections)


def test_paged_catalog_retains_more_than_server_page_limit(monkeypatch) -> None:
    connections = install_directory(monkeypatch, count=1205)
    identities = ldap.LdapDirectory(settings()).list_users()
    assert len(identities) == 1205
    assert len({item.external_subject for item in identities}) == 1205
    assert identities[0].username == "employee-0000"
    assert identities[-1].username == "employee-1204"
    assert connections[0].closed


def test_directory_snapshot_preserves_nested_and_empty_organization_units(monkeypatch) -> None:
    connections = install_directory(monkeypatch, count=2)
    snapshot = ldap.LdapDirectory(settings()).directory_snapshot()

    organizations = {item.name: item for item in snapshot.organizations}
    assert set(organizations) == {"People", "Engineering", "Platform", "Research"}
    assert organizations["People"].parent_id is None
    assert organizations["Research"].parent_id is None
    assert organizations["Platform"].parent_id == organizations["Engineering"].id
    assert {item.username: item.organization_id for item in snapshot.users} == {
        "employee-0000": organizations["Platform"].id,
        "employee-0001": organizations["People"].id,
    }
    assert all("dc=" not in item.id for item in snapshot.organizations)
    assert connections[0].closed


def test_starttls_failure_closes_connection_without_plaintext_fallback(monkeypatch) -> None:
    connections = install_directory(monkeypatch)
    with pytest.raises(DomainError) as failure:
        ldap.LdapDirectory(settings(ldap_start_tls=True)).list_users()
    assert failure.value.code == "LDAP_UNAVAILABLE"
    assert connections[0].closed
    assert not connections[0].bound


def test_ldaps_keeps_certificate_validation_even_when_starttls_disabled(monkeypatch) -> None:
    connections = install_directory(monkeypatch)
    ldap.LdapDirectory(settings(ldap_url="ldaps://directory.example.test:636")).list_users()
    server = connections[0].server
    assert server.ssl is True
    assert server.tls.validate == ssl.CERT_REQUIRED


def test_failed_search_does_not_return_partial_catalog(monkeypatch) -> None:
    connections = install_directory(monkeypatch)
    directory = ldap.LdapDirectory(settings(ldap_user_list_filter="broken-filter"))
    with pytest.raises(DomainError) as failure:
        directory.list_users()
    assert failure.value.code == "LDAP_UNAVAILABLE"
    assert connections[0].closed


def test_real_socket_accepts_configured_fractional_receive_timeout() -> None:
    with socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def accept() -> None:
            with listener.accept()[0] as client:
                client.settimeout(5)
                while client.recv(1024):
                    pass

        thread = Thread(target=accept, daemon=True)
        thread.start()
        directory = ldap.LdapDirectory(
            settings(
                ldap_url=f"ldap://127.0.0.1:{listener.getsockname()[1]}",
                ldap_receive_timeout_seconds=0.5,
            )
        )
        connection = directory._connection(user="test-reader", password="test-password")
        try:
            assert not connection.closed
            assert connection.receive_timeout == 1
        finally:
            connection.unbind()
            thread.join(timeout=5)
        assert not thread.is_alive()
