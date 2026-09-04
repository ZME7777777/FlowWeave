from __future__ import annotations

from typing import cast

import httpx

from flowweave.modules.model_providers.application.service import ProviderConnectionSnapshot
from flowweave.shared.errors import DomainError


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _budget_body(payload: object) -> dict[object, object] | None:
    """Find LiteLLM's key-budget record without relying on key identifiers."""

    if not isinstance(payload, dict):
        return None
    body = cast(dict[object, object], payload)
    info = body.get("info")
    if isinstance(info, dict):
        return cast(dict[object, object], info)
    data = body.get("data")
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return cast(dict[object, object], data[0])
    if "spend" in body or "max_budget" in body:
        return body
    return None


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


async def read_provider_budget(
    client: httpx.AsyncClient, snapshot: ProviderConnectionSnapshot
) -> dict[str, object]:
    """Read an optional LiteLLM virtual-key budget.

    OpenAI-compatible inference APIs have no standard usage endpoint.  LiteLLM
    exposes a non-mutating ``/key/info`` endpoint on deployments that permit a
    virtual key to read its own record.  Unsupported, unauthorised, malformed,
    and network-failed probes deliberately collapse to one safe UI state.
    """

    try:
        # Most gateways expose OpenAI-compatible inference below ``/v1`` but
        # retain LiteLLM management endpoints at the origin.  Prefer the
        # configured path, then use the origin only for that common layout.
        paths = [f"{snapshot.base_url}/key/info"]
        if snapshot.base_url.rstrip("/").endswith("/v1"):
            paths.append(f"{snapshot.base_url.rstrip('/')[:-3]}/key/info")
        response: httpx.Response | None = None
        for path in paths:
            response = await client.get(path, headers=snapshot.headers, timeout=10)
            if response.status_code == 200:
                break
        if response is None or response.status_code != 200:
            return {"status": "UNAVAILABLE", "reason": "上游未开放 API Key 用量查询"}
        record = _budget_body(response.json())
        if record is None:
            return {"status": "UNAVAILABLE", "reason": "上游未返回可识别的预算信息"}
        spend = _number(record.get("spend"))
        max_budget = _number(record.get("max_budget"))
        if spend is None:
            return {"status": "UNAVAILABLE", "reason": "上游未返回已用预算"}
        if max_budget is None:
            return {"status": "UNLIMITED", "spend": spend}
        return {
            "status": "AVAILABLE",
            "spend": spend,
            "max_budget": max_budget,
            "remaining": max(max_budget - spend, 0.0),
        }
    except (httpx.HTTPError, TypeError, ValueError):
        return {"status": "UNAVAILABLE", "reason": "暂时无法读取上游用量"}
