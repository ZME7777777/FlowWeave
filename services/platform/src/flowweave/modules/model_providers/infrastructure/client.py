from __future__ import annotations

from typing import cast

import httpx

from flowweave.modules.model_providers.application.service import ProviderConnectionSnapshot
from flowweave.shared.errors import DomainError


async def discover_provider_models(
    client: httpx.AsyncClient, snapshot: ProviderConnectionSnapshot
) -> list[str]:
    """Perform provider I/O without holding a database transaction."""

    try:
        response = await client.get(
            f"{snapshot.base_url}/models",
            headers=snapshot.headers,
            timeout=10,
        )
        response.raise_for_status()
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise ValueError("provider response must be an object")
        body = cast(dict[object, object], payload)
        values = body.get("data", [])
        if not isinstance(values, list):
            raise ValueError("provider model list must be an array")
        models: set[str] = set()
        for raw_item in cast(list[object], values):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[object, object], raw_item)
            value = item.get("id") or item.get("name")
            if isinstance(value, str | int | float):
                models.add(str(value))
        return sorted(models)
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise DomainError("EXECUTOR_UNAVAILABLE", "model discovery failed", 503) from exc
