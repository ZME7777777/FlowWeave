from __future__ import annotations

from typing import Any, cast

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeEventType,
    RuntimeHandle,
    RuntimeResult,
    StartAttemptRequest,
)
from flowweave.shared.errors import DomainError

_EVENT_TYPES: dict[str, RuntimeEventType] = {
    "MESSAGE": "MESSAGE",
    "MESSAGE_ADDED": "MESSAGE",
    "AGENT_MESSAGE": "MESSAGE",
    "TOOL": "TOOL",
    "TOOL_CALL": "TOOL",
    "TOOL_RESULT": "TOOL",
    "STATE": "STATE",
    "STATUS": "STATE",
    "OUTPUT": "OUTPUT",
    "ERROR": "ERROR",
    "FAILED": "ERROR",
    "WAITING_FOR_USER": "HUMAN_INPUT_REQUIRED",
    "WAITING_HUMAN": "HUMAN_INPUT_REQUIRED",
    "HUMAN_INPUT_REQUIRED": "HUMAN_INPUT_REQUIRED",
    "FINISHED": "COMPLETED",
    "COMPLETED": "COMPLETED",
    "STOPPED": "COMPLETED",
}


class OpenHandsRuntime:
    """Protocol adapter that normalizes OpenHands state and incremental events."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.openhands_base_url.rstrip("/")
        self.headers = {"X-Session-API-Key": settings.openhands_session_api_key}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(
                    method, f"{self.base_url}{path}", headers=self.headers, **kwargs
                )
                response.raise_for_status()
                value = cast(object, response.json())
                if not isinstance(value, dict):
                    raise ValueError("OpenHands response must be an object")
                return cast(dict[str, Any], value)
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError("EXECUTOR_UNAVAILABLE", "OpenHands is unavailable", 503) from exc

    def start(self, request: StartAttemptRequest) -> RuntimeHandle:
        asset = cast(dict[str, Any], request.node["asset"])
        executor = cast(dict[str, Any], asset.get("executor") or {})
        payload: dict[str, Any] = {
            "initial_message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": executor.get("startup_prompt")
                        or f"Execute node: {request.node['asset']['name']}",
                    }
                ],
            },
            "workspace": {"path": request.workspace_ref},
            "metadata": {"execution_key": request.execution_key},
        }
        created = self._request("POST", "/api/conversations", json=payload)
        conversation_id = str(created.get("id") or created.get("conversation_id"))
        if not conversation_id or conversation_id == "None":
            raise DomainError("RUNTIME_PROTOCOL_ERROR", "Missing conversation id", 502)
        self._request("POST", f"/api/conversations/{conversation_id}/run", json={})
        return RuntimeHandle(job_id=conversation_id, conversation_id=conversation_id)

    @staticmethod
    def _cursor(value: object, fallback: str) -> str:
        rendered = str(value) if value is not None else fallback
        return rendered or fallback

    @staticmethod
    def _payload(value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        return {"value": value}

    @staticmethod
    def _outputs(value: object) -> dict[str, tuple[str, str]]:
        if not isinstance(value, dict):
            return {}
        outputs: dict[str, tuple[str, str]] = {}
        for raw_key, raw_value in cast(dict[object, object], value).items():
            key = str(raw_key)
            if isinstance(raw_value, dict):
                item = cast(dict[str, object], raw_value)
                outputs[key] = (
                    str(item.get("artifact_type") or item.get("type") or "TEXT"),
                    str(item.get("content") or item.get("value") or ""),
                )
            elif isinstance(raw_value, list):
                pair = cast(list[object], raw_value)
                if len(pair) == 2:
                    outputs[key] = (str(pair[0]), str(pair[1]))
                else:
                    outputs[key] = ("TEXT", str(cast(object, pair)))
            else:
                outputs[key] = ("TEXT", str(raw_value))
        return outputs

    def _result_from_status(self, data: dict[str, Any], cursor: str | None) -> RuntimeResult:
        status = str(data.get("status", "RUNNING")).upper()
        if status in {"FINISHED", "COMPLETED", "STOPPED"}:
            outputs = self._outputs(data.get("outputs"))
            if not outputs:
                content = str(data.get("final_response") or data.get("output") or "")
                outputs = {"result": ("TEXT", content)}
            return RuntimeResult(status="COMPLETED", outputs=outputs, cursor=cursor)
        if status in {"WAITING_FOR_USER", "WAITING_HUMAN", "HUMAN_INPUT_REQUIRED"}:
            return RuntimeResult(
                status="HUMAN_INPUT_REQUIRED",
                human_question=str(data.get("question") or data.get("message") or ""),
                cursor=cursor,
            )
        if status in {"FAILED", "ERROR"}:
            return RuntimeResult(
                status="FAILED", error=str(data.get("error") or status), cursor=cursor
            )
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        params = {"after": handle.cursor} if handle.cursor else None
        data = self._request(
            "GET", f"/api/conversations/{handle.conversation_id}/events", params=params
        )
        raw_events = data.get("events", data.get("items", []))
        events: list[RuntimeEvent] = []
        cursor = handle.cursor
        if isinstance(raw_events, list):
            for index, raw in enumerate(cast(list[object], raw_events), start=1):
                if not isinstance(raw, dict):
                    payload: dict[str, Any] = {"value": raw}
                    raw_type = "UNKNOWN"
                    event_cursor = self._cursor(None, f"{cursor or '0'}:{index}")
                else:
                    item = cast(dict[str, object], raw)
                    raw_type = str(
                        item.get("event_type") or item.get("type") or item.get("kind") or "UNKNOWN"
                    ).upper()
                    event_cursor = self._cursor(
                        item.get("cursor") or item.get("id") or item.get("sequence"),
                        f"{cursor or '0'}:{index}",
                    )
                    payload = self._payload(item.get("payload", item.get("data", item)))
                event_type = _EVENT_TYPES.get(raw_type, "UNKNOWN")
                events.append(
                    RuntimeEvent(
                        event_cursor,
                        event_type,
                        {"source_type": raw_type, **payload},
                    )
                )
                cursor = event_cursor
        cursor_value = data.get("cursor") or data.get("next_cursor")
        if cursor_value is not None:
            cursor = self._cursor(cursor_value, cursor or "") or None
        result = self._result_from_status(data, cursor)
        return RuntimeEventBatch(
            tuple(events),
            cursor,
            result if result.status != "RUNNING" else None,
        )

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        data = self._request("GET", f"/api/conversations/{handle.conversation_id}")
        cursor_value = data.get("cursor")
        cursor = str(cursor_value) if cursor_value is not None else handle.cursor
        return self._result_from_status(data, cursor)

    def resume(self, handle: RuntimeHandle, content: str) -> RuntimeResult:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/messages",
            json={"content": content},
        )
        return self.inspect(handle)

    def cancel(self, handle: RuntimeHandle) -> None:
        self._request("POST", f"/api/conversations/{handle.conversation_id}/stop", json={})
