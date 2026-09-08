from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

import httpx


@dataclass(slots=True)
class HttpTransportPool:
    """Bounded process-lifetime clients for regular and control traffic."""

    regular: httpx.Client
    control: httpx.Client
    async_regular: httpx.AsyncClient
    async_control: httpx.AsyncClient

    @classmethod
    def build(cls) -> HttpTransportPool:
        timeout = httpx.Timeout(connect=5, read=30, write=30, pool=5)
        control_timeout = httpx.Timeout(connect=5, read=None, write=30, pool=5)
        return cls(
            regular=httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
                follow_redirects=False,
            ),
            control=httpx.Client(
                timeout=control_timeout,
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=2,
                    keepalive_expiry=15,
                ),
                follow_redirects=False,
            ),
            async_regular=httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
                follow_redirects=False,
            ),
            async_control=httpx.AsyncClient(
                timeout=control_timeout,
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=2,
                    keepalive_expiry=15,
                ),
                follow_redirects=False,
            ),
        )

    async def aclose(self) -> None:
        await self.async_regular.aclose()
        await self.async_control.aclose()
        self.regular.close()
        self.control.close()


_pools: dict[tuple[str, str, str], HttpTransportPool] = {}
_pools_lock = Lock()


def _settings_key(settings: object) -> tuple[str, str, str]:
    return (
        str(getattr(settings, "docker_controller_url", "")),
        str(getattr(settings, "docker_controller_api_key", "")),
        str(getattr(settings, "sandbox_manager_scope", "")),
    )


def shared_http_transport(settings: object) -> HttpTransportPool:
    key = _settings_key(settings)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = HttpTransportPool.build()
            _pools[key] = pool
        return pool


def register_http_transport(settings: object, pool: HttpTransportPool) -> None:
    key = _settings_key(settings)
    with _pools_lock:
        _pools[key] = pool


def unregister_http_transport(settings: object, pool: HttpTransportPool) -> None:
    key = _settings_key(settings)
    with _pools_lock:
        if _pools.get(key) is pool:
            _pools.pop(key, None)
