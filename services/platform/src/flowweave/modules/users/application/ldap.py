from __future__ import annotations

import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from flowweave.bootstrap.settings import Settings
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class LdapIdentity:
    external_subject: str
    username: str
    display_name: str
    email: str | None


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
            connection.unbind()
        return identity[1]

    def list_users(self) -> list[LdapIdentity]:
        connection = self._service_connection()
        try:
            entries = connection.extend.standard.paged_search(
                self._settings.ldap_base_dn,
                self._settings.ldap_user_list_filter,
                search_scope=SUBTREE,
                attributes=["uid", "cn", "mail", "entryUUID"],
                paged_size=500,
                paged_criticality=True,
                generator=False,
            )
            if not connection.result or connection.result["result"] != 0:
                raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503)
            users = [
                self._identity(entry["dn"], entry["raw_attributes"])
                for entry in entries
                if entry["type"] == "searchResEntry"
            ]
            return sorted(
                users,
                key=lambda item: (item.display_name.casefold(), item.username.casefold()),
            )
        except LDAPException as error:
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        finally:
            connection.unbind()

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
            connection.unbind()

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
            receive_timeout=self._settings.ldap_receive_timeout_seconds,
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
            connection.unbind()
            raise DomainError("LDAP_UNAVAILABLE", "LDAP 目录当前不可用，请稍后重试", 503) from error
        return connection

    @staticmethod
    def _identity(dn: str, attributes: dict[str, object]) -> LdapIdentity:
        def value(name: str) -> str | None:
            raw = attributes.get(name)
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
        )

    @staticmethod
    def _authentication_failed() -> DomainError:
        return DomainError("AUTHENTICATION_FAILED", "用户名或密码错误", 401)


__all__ = ("LdapDirectory", "LdapIdentity")
