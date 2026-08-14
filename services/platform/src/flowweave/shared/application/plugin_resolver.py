from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PluginResolveRequest:
    """A credential-free, immutable Git source selected for publication."""

    source: str
    commit: str
    repo_path: str | None = None


@dataclass(frozen=True, slots=True)
class MarketplacePluginResolveRequest:
    """A Plugin selected from one immutable OpenHands Marketplace snapshot."""

    marketplace_source: str
    marketplace_commit: str
    marketplace_repo_path: str | None
    plugin_name: str


@dataclass(frozen=True, slots=True)
class MarketplaceCatalogRequest:
    """One credential-free, immutable Marketplace snapshot to browse."""

    marketplace_source: str
    marketplace_commit: str
    marketplace_repo_path: str | None


@dataclass(frozen=True, slots=True)
class PluginResolveBundle:
    """Canonical Plugin ZIP returned by the isolated OpenHands resolver."""

    content: bytes
    resolved_commit: str
    report: dict[str, object]
    resolved_source: str | None = None
    resolved_repo_path: str | None = None


class PluginResolverPort(Protocol):
    def resolve(self, request: PluginResolveRequest) -> PluginResolveBundle: ...

    def resolve_marketplace_plugin(
        self, request: MarketplacePluginResolveRequest
    ) -> PluginResolveBundle: ...

    def list_marketplace(self, request: MarketplaceCatalogRequest) -> dict[str, object]: ...
