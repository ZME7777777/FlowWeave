from __future__ import annotations

import hashlib
import json
from typing import Any

RUNTIME_CONFIG_ENVELOPE_KEYS = frozenset(
    {
        "capability_id",
        "capability_version_id",
        "package_id",
        "version_no",
        "digest",
        "filename",
        "content_hash",
        "storage_key",
    }
)


def normalized_capability_config(runtime_config: dict[str, Any]) -> dict[str, Any]:
    """Remove repository identity fields from a materialized Runtime config."""

    return {
        key: value
        for key, value in runtime_config.items()
        if key not in RUNTIME_CONFIG_ENVELOPE_KEYS
    }


def capability_version_digest(
    capability_type: str,
    capability_key: str,
    content_hash: str,
    normalized_config: dict[str, Any],
) -> str:
    encoded = json.dumps(
        {
            "capability_type": capability_type,
            "capability_key": capability_key,
            "content_hash": content_hash,
            "normalized_config": normalized_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
