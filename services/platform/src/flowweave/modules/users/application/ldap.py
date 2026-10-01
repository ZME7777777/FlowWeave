from __future__ import annotations

import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import ceil
from typing import Any, cast
from urllib.parse import urlsplit

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn  # pyright: ignore[reportUnknownVariableType]

from flowweave.bootstrap.settings import Settings
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class LdapIdentity:
    external_subject: str
    username: str
    display_name: str
    email: str | None
    organization_id: str | None = None
    organization_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LdapOrganization:
    id: str
    parent_id: str | None
    name: str


@dataclass(frozen=True, slots=True)
class LdapDirectorySnapshot:
    users: list[LdapIdentity]
    organizations: list[LdapOrganization]


class LdapDirectory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        identity = self._find_by_uid(username)
        connection = self._connection(user=identity[0], password=password)
        try:
            if not connection.bind():
                raise self._authentication_failed()
        except LDAPException as error:
            raise self._authentication_failed() from error
        finally:
            self._unbind(connection)
        return identity[1]

    def list_users(self) -> list[LdapIdentity]:
        connection = self._service_connection()
        try:
            return self._list_users(connection)
        except LDAPException as error:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        finally:
            self._unbind(connection)

    def directory_snapshot(self) -> LdapDirectorySnapshot:
        connection = self._service_connection()
        try:
            users = self._list_users(connection)
            entries = self._paged_entries(
                connection,
                "(objectClass=organizationalUnit)",
                attributes=["ou"],
            )
            organizations = self._organizations(entries)
            organization_ids = {item.id for item in organizations}
            return LdapDirectorySnapshot(
                users=[
                    LdapIdentity(
                        external_subject=item.external_subject,
                        username=item.username,
                        display_name=item.display_name,
                        email=item.email,
                        organization_id=(
                            item.organization_id
                            if item.organization_id in organization_ids
                            else None
                        ),
                        organization_ids=tuple(
                            organization_id
                            for organization_id in item.organization_ids
                            if organization_id in organization_ids
                        ),
                    )
                    for item in users
                ],
                organizations=organizations,
            )
        except LDAPException as error:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        finally:
            self._unbind(connection)

    def _list_users(self, connection: Connection) -> list[LdapIdentity]:
        entries = self._paged_entries(
            connection,
            self._settings.ldap_user_list_filter,
            attributes=["uid", "cn", "mail", "entryUUID"],
        )
        users = [self._identity(entry["dn"], entry["raw_attributes"]) for entry in entries]
        return sorted(
            users,
            key=lambda item: (item.display_name.casefold(), item.username.casefold()),
        )

    def _paged_entries(
        self,
        connection: Connection,
        search_filter: str,
        *,
        attributes: list[str],
    ) -> list[dict[str, Any]]:
        entries = cast(
            list[dict[str, Any]],
            connection.extend.standard.paged_search(
                self._settings.ldap_base_dn,
                search_filter,
                search_scope=SUBTREE,
                attributes=attributes,
                paged_size=500,
                paged_criticality=True,
                generator=False,
            ),
        )
        if not connection.result or connection.result["result"] != 0:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503)
        return [entry for entry in entries if entry["type"] == "searchResEntry"]

    def _find_by_uid(self, username: str) -> tuple[str, LdapIdentity]:
        normalized = username.strip()
        if not normalized:
            raise self._authentication_failed()
        connection = self._service_connection()
        try:
            search_filter = self._settings.ldap_user_search_filter.replace(
                "{uid}", escape_filter_chars(normalized)
            )
            connection.search(
                self._settings.ldap_base_dn,
                search_filter,
                search_scope=SUBTREE,
                attributes=["uid", "cn", "mail", "entryUUID"],
                size_limit=2,
            )
            if len(connection.entries) != 1:
                raise self._authentication_failed()
            entry = connection.entries[0]
            return entry.entry_dn, self._identity(entry.entry_dn, entry.entry_raw_attributes)
        except LDAPException as error:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        finally:
            self._unbind(connection)

    def _service_connection(self) -> Connection:
        connection = self._connection(
            user=self._settings.ldap_search_bind_dn,
            password=self._settings.ldap_search_bind_password,
        )
        try:
            if not connection.bind():
                raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503)
        except LDAPException as error:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        return connection

    def _connection(self, *, user: str, password: str) -> Connection:
        parsed = urlsplit(self._settings.ldap_url)
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=self._settings.ldap_tls_ca_cert_file or None,
        )
        server = Server(
            parsed.hostname or "",
            port=parsed.port or (636 if parsed.scheme == "ldaps" else 389),
            use_ssl=parsed.scheme == "ldaps",
            tls=tls,
            connect_timeout=self._settings.ldap_connect_timeout_seconds,
            get_info=NONE,
        )
        connection = Connection(
            server,
            user=user,
            password=password,
            # ldap3 packs this timeout into a timeval requiring whole seconds.
            receive_timeout=ceil(self._settings.ldap_receive_timeout_seconds),
            raise_exceptions=False,
        )
        try:
            # ldap3's synchronous open() returns None even on success.
            connection.open()
            if connection.closed:
                raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503)
            requires_start_tls = parsed.scheme == "ldap" and self._settings.ldap_start_tls
            if requires_start_tls and not connection.start_tls():
                raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503)
        except (LDAPException, DomainError) as error:
            self._unbind(connection)
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        return connection

    @staticmethod
    def _unbind(connection: Connection) -> None:
        connection.unbind()  # pyright: ignore[reportUnknownMemberType]

    @staticmethod
    def _identity(dn: str, attributes: Mapping[str, Any]) -> LdapIdentity:
        def value(name: str) -> str | None:
            raw = cast(object, attributes.get(name))
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise DomainError(
                        "LDAP_DIRECTORY_INVALID", "LDAP 用户目录身份属性编码无效", 503
                    ) from error
            return str(raw).strip() if raw is not None and str(raw).strip() else None

        username = value("uid")
        subject = value("entryUUID")
        if username is None or subject is None:
            raise DomainError("LDAP_DIRECTORY_INVALID", "LDAP 用户目录缺少必要的身份属性", 503)
        return LdapIdentity(
            external_subject=subject,
            username=username,
            display_name=value("cn") or username,
            email=value("mail"),
            organization_id=LdapDirectory._organization_id(LdapDirectory._parent_dn(dn)),
            organization_ids=LdapDirectory._organization_ids(dn),
        )

    @staticmethod
    def _organizations(entries: list[dict[str, Any]]) -> list[LdapOrganization]:
        directory_rows: list[tuple[str, str, str]] = []
        for entry in entries:
            dn = str(entry["dn"])
            raw = cast(object, entry["raw_attributes"])
            if not isinstance(raw, Mapping):
                continue
            attributes = cast(Mapping[str, object], raw)
            value = attributes.get("ou")
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise DomainError(
                        "LDAP_DIRECTORY_INVALID", "LDAP 组织目录名称编码无效", 503
                    ) from error
            name = str(value).strip() if value is not None else ""
            if name:
                directory_rows.append((dn, LdapDirectory._organization_id(dn) or "", name))
        by_dn = {dn.casefold(): organization_id for dn, organization_id, _name in directory_rows}
        return sorted(
            (
                LdapOrganization(
                    id=organization_id,
                    parent_id=by_dn.get((LdapDirectory._parent_dn(dn) or "").casefold()),
                    name=name,
                )
                for dn, organization_id, name in directory_rows
            ),
            key=lambda item: (item.name.casefold(), item.id),
        )

    @staticmethod
    def _parent_dn(dn: str) -> str | None:
        parts = cast(list[tuple[str, str, str]], parse_dn(dn, escape=True))
        if len(parts) < 2:
            return None
        first_rdn_end = 0
        while first_rdn_end < len(parts) and parts[first_rdn_end][2] == "+":
            first_rdn_end += 1
        parent = parts[first_rdn_end + 1 :]
        if not parent:
            return None
        return "".join(f"{name}={value}{separator}" for name, value, separator in parent)

    @staticmethod
    def _organization_id(dn: str | None) -> str | None:
        if not dn:
            return None
        return "ldap-org-" + sha256(dn.casefold().encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _organization_ids(dn: str) -> tuple[str, ...]:
        result: list[str] = []
        current = LdapDirectory._parent_dn(dn)
        visited: set[str] = set()
        while current and current.casefold() not in visited:
            visited.add(current.casefold())
            organization_id = LdapDirectory._organization_id(current)
            if organization_id is not None:
                result.append(organization_id)
            current = LdapDirectory._parent_dn(current)
        return tuple(result)

    @staticmethod
    def _authentication_failed() -> DomainError:
        return DomainError("AUTHENTICATION_FAILED", "用户名或密码错误", 401)


__all__ = (
    "LdapDirectory",
    "LdapDirectorySnapshot",
    "LdapIdentity",
    "LdapOrganization",
)
