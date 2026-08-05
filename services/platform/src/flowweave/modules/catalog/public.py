"""Stable public facade for the catalog module."""

from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_imports import cleanup_expired_import
from flowweave.modules.catalog.application.service import asset_dict
from flowweave.shared.models import NodeAsset


def cleanup_capability_import(db: Session, import_id: str) -> None:
    """Expire an import session and reclaim its unreferenced source object."""

    cleanup_expired_import(db, import_id)


def describe_asset(db: Session, asset: NodeAsset) -> dict[str, object]:
    """Return the stable read model used by flow snapshots."""

    return asset_dict(db, asset)


__all__ = ("cleanup_capability_import", "describe_asset")
