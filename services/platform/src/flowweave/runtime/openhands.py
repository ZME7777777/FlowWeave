from __future__ import annotations

import json
from pathlib import Path
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


class OpenHandsRuntime:
    """OpenHands Agent Server adapter backed by the node's configured model provider."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.openhands_base_url.rstrip("/")
        self.headers = {"X-Session-API-Key": settings.openhands_session_api_key}
        self.workspace_root = settings.workspace_root.resolve()
        self.openhands_workspace_root = settings.openhands_workspace_root
        self._contracts: dict[str, list[dict[str, str]]] = {}

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
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands Agent Server is unavailable or rejected the request",
                503,
            ) from exc

    @staticmethod
    def _model_name(model: str) -> str:
        return model if "/" in model else f"openai/{model}"

    def _workspace_path(self, value: str) -> str:
        path = Path(value)
        try:
            relative = path.resolve().relative_to(self.workspace_root)
        except ValueError:
            relative = Path(path.name)
        return str(self.openhands_workspace_root / relative)

    @staticmethod
    def _artifact_input(binding: dict[str, Any]) -> dict[str, Any]:
        artifact = cast(dict[str, Any], binding.get("artifact") or {})
        return {
            "field_key": binding.get("field_key"),
            "artifact_type": artifact.get("artifact_type"),
            "inline_content": artifact.get("inline_content"),
            "uri": artifact.get("uri"),
            "metadata": artifact.get("metadata", {}),
        }

    @staticmethod
    def _output_contract(request: StartAttemptRequest) -> list[dict[str, str]]:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        raw_outputs: object = asset.get("outputs") or []
        if not isinstance(raw_outputs, list):
            return []
        outputs = cast(list[object], raw_outputs)
        return [
            {
                "field_key": str(item.get("field_key") or "result"),
                "artifact_type": str(item.get("data_type") or "TEXT"),
                "description": str(item.get("description") or ""),
            }
            for raw in outputs
            if isinstance(raw, dict)
            for item in [cast(dict[str, Any], raw)]
        ]

    def _initial_text(self, request: StartAttemptRequest) -> str:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        startup = str(executor.get("startup_prompt") or "").strip()
        context = str(executor.get("context_prompt") or "").strip()
        inputs = [self._artifact_input(item) for item in request.bindings]
        outputs = self._output_contract(request)
        sections = [
            startup or f"执行节点：{asset.get('name') or request.node.get('instance_key')}",
        ]
        if context:
            sections.append(f"任务上下文：\n{context}")
        if request.node_workspace_ref:
            resource_lines = [
                f"节点持久工作目录：{request.node_workspace_ref}",
                f"文本与附件目录：{request.node_workspace_ref}/files",
                f"代码仓库目录：{request.node_workspace_ref}/repositories",
            ]
            if request.skills:
                resource_lines.append("可用 Skills：")
                resource_lines.extend(
                    f"- {skill.name}: {skill.source}（脚本目录 {skill.workspace_path}）"
                    for skill in request.skills
                )
            if request.mcp_servers:
                resource_lines.append("可用 MCP Servers：")
                resource_lines.extend(
                    f"- {server.name}: {server.workspace_path}" for server in request.mcp_servers
                )
            resource_lines.append(
                "用户在消息中显式选择 Skill 或 MCP 时，必须优先调用所选能力；"
                "Skill 附带脚本可直接从上述目录执行。"
            )
            sections.append("运行资源：\n" + "\n".join(resource_lines))
        sections.append("流程输入：\n" + json.dumps(inputs, ensure_ascii=False, default=str))
        if outputs:
            sections.append(
                "完成任务后，请调用 finish，并将 message 严格写成 JSON 对象。"
                "对象的 key 必须是下列 field_key；每个 value 必须包含 artifact_type 和 content。"
                "不要使用 Markdown 代码围栏。\n" + json.dumps(outputs, ensure_ascii=False)
            )
        return "\n\n".join(sections)

    def _create(self, request: StartAttemptRequest, *, run: bool) -> RuntimeHandle:
        provider = request.provider
        if provider is None:
            raise DomainError(
                "MODEL_PROVIDER_REQUIRED",
                "The node executor must select a model provider before it can run",
                422,
            )
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        skills = [
            {
                "name": skill.name,
                "content": skill.content,
                "description": skill.description or None,
                "source": skill.source or None,
                "is_agentskills_format": False,
            }
            for skill in request.skills
        ]
        agent: dict[str, Any] = {
            "kind": "Agent",
            "llm": {
                "model": self._model_name(provider.model),
                "base_url": provider.base_url,
                "api_key": provider.api_key,
                "usage_id": f"flowweave:{provider.provider_id}",
            },
            "tools": [
                {"name": "terminal", "params": {}},
                {"name": "file_editor", "params": {}},
                {"name": "task_tracker", "params": {}},
            ],
        }
        if skills:
            agent["agent_context"] = {"skills": skills}
        if request.mcp_servers:
            agent["mcp_config"] = {server.name: server.config for server in request.mcp_servers}
        payload: dict[str, Any] = {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": self._workspace_path(request.workspace_ref),
            },
            "max_iterations": int(executor.get("max_iterations") or 100),
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": self._initial_text(request)}],
                "run": run,
            },
            "agent": agent,
        }
        created = self._request("POST", "/api/conversations", json=payload)
        conversation_id = str(created.get("id") or "")
        if not conversation_id:
            raise DomainError("RUNTIME_PROTOCOL_ERROR", "Missing conversation id", 502)
        self._contracts[conversation_id] = self._output_contract(request)
        cursor_value = created.get("leaf_event_id") or created.get("last_user_message_id")
        cursor = str(cursor_value) if cursor_value else None
        return RuntimeHandle(job_id=conversation_id, conversation_id=conversation_id, cursor=cursor)

    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=False)

    def start(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=True)

    @staticmethod
    def _text_content(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        values: list[str] = []
        for raw in cast(list[object], value):
            if isinstance(raw, dict):
                item = cast(dict[str, object], raw)
                text = item.get("text")
                if isinstance(text, str):
                    values.append(text)
        return "\n".join(values)

    @classmethod
    def _event_text(cls, item: dict[str, Any]) -> str:
        kind = str(item.get("kind") or "")
        if kind == "MessageEvent":
            message = item.get("llm_message")
            if isinstance(message, dict):
                return cls._text_content(cast(dict[str, object], message).get("content"))
        if kind == "ActionEvent":
            action = item.get("action")
            if isinstance(action, dict):
                action_item = cast(dict[str, object], action)
                value = action_item.get("message") or action_item.get("thought")
                return str(value) if value is not None else ""
        if kind == "ObservationEvent":
            observation = item.get("observation")
            if isinstance(observation, dict):
                observation_item = cast(dict[str, object], observation)
                value = observation_item.get("content") or observation_item.get("message")
                return str(value) if value is not None else ""
            value = item.get("content") or item.get("message")
            return str(value) if value is not None else ""
        return ""

    @classmethod
    def _event_type(cls, item: dict[str, Any]) -> RuntimeEventType:
        kind = str(item.get("kind") or "")
        if kind == "MessageEvent":
            return "MESSAGE"
        if kind == "ActionEvent":
            action = item.get("action")
            action_kind = (
                str(cast(dict[str, object], action).get("kind") or "")
                if isinstance(action, dict)
                else ""
            )
            if action_kind == "FinishAction":
                return "COMPLETED"
            return "THOUGHT" if action_kind == "ThinkAction" else "TOOL_CALL"
        if kind == "ObservationEvent":
            return "TOOL_RESULT"
        if "error" in kind.lower():
            return "ERROR"
        return "STATE"

    @classmethod
    def _safe_event_detail(cls, value: object, *, depth: int = 0) -> object:
        if depth >= 6:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key): (
                    "[redacted]"
                    if any(
                        marker in str(key).lower()
                        for marker in ("api_key", "authorization", "password", "secret", "token")
                    )
                    else cls._safe_event_detail(child, depth=depth + 1)
                )
                for key, child in list(cast(dict[object, object], value).items())[:100]
            }
        if isinstance(value, list):
            sequence = cast(list[object], value)
            return [cls._safe_event_detail(item, depth=depth + 1) for item in sequence[:100]]
        if isinstance(value, str):
            return value[:20_000]
        return value if value is None or isinstance(value, int | float | bool) else str(value)

    @classmethod
    def _event_payload(cls, item: dict[str, Any]) -> dict[str, Any]:
        kind = str(item.get("kind") or "UNKNOWN")
        payload: dict[str, Any] = {
            "source_type": kind,
            "source": item.get("source"),
            "content": cls._event_text(item),
        }
        raw_detail = (
            item.get("action")
            if kind == "ActionEvent"
            else item.get("observation")
            if kind == "ObservationEvent"
            else None
        )
        if isinstance(raw_detail, dict):
            detail = cast(dict[str, Any], raw_detail)
            payload["event_name"] = str(detail.get("kind") or kind)
            payload["details"] = cls._safe_event_detail(
                {
                    key: value
                    for key, value in detail.items()
                    if key not in {"kind", "message", "thought"}
                }
            )
        return payload

    def _events(
        self, conversation_id: str, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"limit": 100, "sort_order": "TIMESTAMP"}
        if cursor:
            params["page_id"] = cursor
        data = self._request(
            "GET", f"/api/conversations/{conversation_id}/events/search", params=params
        )
        raw_items: object = data.get("items", [])
        items = (
            [
                cast(dict[str, Any], item)
                for item in cast(list[object], raw_items)
                if isinstance(item, dict)
            ]
            if isinstance(raw_items, list)
            else []
        )
        next_cursor = str(items[-1].get("id")) if items and items[-1].get("id") else cursor
        return items, next_cursor

    @staticmethod
    def _json_object(text: str) -> dict[str, Any] | None:
        value = text.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if len(lines) >= 3:
                value = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None

    def _outputs(self, conversation_id: str, text: str) -> dict[str, tuple[str, str]]:
        parsed = self._json_object(text)
        contract = self._contracts.get(conversation_id, [])
        by_key = {item["field_key"]: item for item in contract}
        if parsed is not None:
            outputs: dict[str, tuple[str, str]] = {}
            for key, raw in parsed.items():
                expected = by_key.get(str(key), {})
                if isinstance(raw, dict):
                    item = cast(dict[str, object], raw)
                    artifact_type = str(
                        item.get("artifact_type") or expected.get("artifact_type") or "TEXT"
                    )
                    content = str(item.get("content") or item.get("value") or "")
                else:
                    artifact_type = str(expected.get("artifact_type") or "TEXT")
                    content = str(raw)
                outputs[str(key)] = (artifact_type, content)
            if outputs:
                return outputs
        if len(contract) == 1:
            item = contract[0]
            return {item["field_key"]: (item["artifact_type"], text)}
        return {"result": ("TEXT", text)} if text else {}

    def _result_from_events(
        self, conversation_id: str, items: list[dict[str, Any]], cursor: str | None
    ) -> RuntimeResult | None:
        for item in reversed(items):
            if self._event_type(item) == "COMPLETED":
                text = self._event_text(item)
                return RuntimeResult(
                    status="COMPLETED",
                    outputs=self._outputs(conversation_id, text),
                    cursor=cursor,
                )
            if self._event_type(item) == "ERROR":
                return RuntimeResult(
                    status="FAILED",
                    error=self._event_text(item) or "OpenHands failed",
                    cursor=cursor,
                )
        return None

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        items, cursor = self._events(handle.conversation_id, handle.cursor)
        events = tuple(
            RuntimeEvent(
                cursor=str(item.get("id") or f"{cursor or '0'}:{index}"),
                event_type=self._event_type(item),
                payload=self._event_payload(item),
            )
            for index, item in enumerate(items, start=1)
        )
        return RuntimeEventBatch(
            events=events,
            cursor=cursor,
            result=self._result_from_events(handle.conversation_id, items, cursor),
        )

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        data = self._request("GET", f"/api/conversations/{handle.conversation_id}")
        status = str(data.get("execution_status") or "running").lower()
        cursor = str(data.get("leaf_event_id") or handle.cursor or "") or None
        if status == "finished":
            items, event_cursor = self._events(handle.conversation_id, None)
            result = self._result_from_events(handle.conversation_id, items, event_cursor or cursor)
            return result or RuntimeResult(
                status="COMPLETED", outputs={}, cursor=event_cursor or cursor
            )
        if status in {"error", "stuck"}:
            return RuntimeResult(
                status="FAILED",
                error=str(data.get("error") or f"OpenHands status: {status}"),
                cursor=cursor,
            )
        if status == "waiting_for_confirmation":
            return RuntimeResult(
                status="HUMAN_INPUT_REQUIRED",
                human_question="Agent 请求人工确认后继续执行",
                cursor=cursor,
            )
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def send_message(self, handle: RuntimeHandle, content: str) -> RuntimeResult:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/events",
            json={
                "role": "user",
                "content": [{"type": "text", "text": content}],
                "run": True,
            },
        )
        return RuntimeResult(status="RUNNING", cursor=handle.cursor)

    def resume(self, handle: RuntimeHandle, content: str) -> RuntimeResult:
        self._request("POST", f"/api/conversations/{handle.conversation_id}/interrupt", json={})
        return self.send_message(handle, content)

    def cancel(self, handle: RuntimeHandle) -> None:
        self._request("POST", f"/api/conversations/{handle.conversation_id}/interrupt", json={})
