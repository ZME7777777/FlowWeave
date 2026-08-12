from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import (
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeEventType,
    RuntimeHandle,
    RuntimeProvider,
    RuntimeResult,
    StartAttemptRequest,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    controller_is_remote,
)


class OpenHandsRuntime:
    """OpenHands Agent Server adapter backed by the node's configured model provider."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.openhands_base_url.rstrip("/")
        self.root_session_api_key = settings.openhands_session_api_key
        self.manager_scope = settings.sandbox_manager_scope
        self.workspace_root = settings.workspace_root.resolve()
        self.openhands_workspace_root = settings.openhands_workspace_root
        self._contracts: dict[str, list[dict[str, str]]] = {}

    @staticmethod
    def _environment_route(job_id: str) -> tuple[str, bool] | None:
        for prefix, disposable in (("env-exec:", True), ("env-chat:", False)):
            if job_id.startswith(prefix):
                container_name = job_id.removeprefix(prefix)
                if container_name:
                    return container_name, disposable
        return None

    def _base_url_for_handle(self, handle: RuntimeHandle) -> str:
        route = self._environment_route(handle.job_id)
        return f"http://{route[0]}:8000" if route else self.base_url

    def _session_key_for_resource(self, resource_name: str | None) -> str:
        if not resource_name:
            return self.root_session_api_key
        return derive_runtime_session_key(
            self.root_session_api_key, self.manager_scope, resource_name
        )

    def _session_key_for_handle(self, handle: RuntimeHandle) -> str:
        route = self._environment_route(handle.job_id)
        return self._session_key_for_resource(route[0] if route else None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        missing_ok: bool = False,
        base_url: str | None = None,
        session_api_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(
                    method,
                    f"{base_url or self.base_url}{path}",
                    headers={"X-Session-API-Key": session_api_key or self.root_session_api_key},
                    **kwargs,
                )
                if missing_ok and response.status_code == 404:
                    return {"_flowweave_missing": True}
                response.raise_for_status()
                value = cast(object, response.json())
                if not isinstance(value, dict):
                    raise ValueError("OpenHands response must be an object")
                return cast(dict[str, Any], value)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and path.startswith("/api/conversations/"):
                raise DomainError(
                    "RUNTIME_CONVERSATION_MISSING",
                    "Agent Runtime conversation no longer exists; retry will rebuild it",
                    409,
                    {"status_code": 404},
                ) from exc
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands Agent Server is unavailable or rejected the request",
                503,
                {"status_code": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "EXECUTOR_UNAVAILABLE",
                "OpenHands Agent Server is unavailable or rejected the request",
                503,
            ) from exc

    @staticmethod
    def _model_name(model: str) -> str:
        return model if "/" in model else f"openai/{model}"

    def _llm_payload(self, provider: RuntimeProvider) -> dict[str, Any]:
        llm: dict[str, Any] = {
            "model": self._model_name(provider.model),
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "usage_id": f"flowweave:{provider.provider_id}",
        }
        if provider.auth_type == "CODEX_OAUTH":
            extra_body: dict[str, Any] = {"store": False}
            if provider.reasoning_effort:
                extra_body["reasoning"] = {"effort": provider.reasoning_effort}
            llm.update(
                {
                    "api_mode": "responses",
                    "extra_headers": provider.extra_headers,
                    "litellm_extra_body": extra_body,
                    "stream": True,
                    "temperature": None,
                    "max_output_tokens": None,
                    "capability_overrides": {
                        "supports_responses_api": True,
                        "supports_sampling_params": False,
                    },
                }
            )
        return llm

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
            "display_name": binding.get("display_name"),
            "description": binding.get("description"),
            "template_url": binding.get("template_url"),
            "artifact_type": artifact.get("artifact_type"),
            "inline_content": artifact.get("inline_content"),
            "uri": artifact.get("uri"),
            "metadata": artifact.get("metadata", {}),
        }

    @staticmethod
    def _output_contract(request: StartAttemptRequest) -> list[dict[str, str]]:
        if request.interaction_mode == "COLLABORATION":
            return []
        return [
            {
                "field_key": field_key,
                "artifact_type": "URL",
                "root_url": target.get("root_url", ""),
                "run_name": target.get("run_name", ""),
                "title": target.get("title", field_key),
                "display_name": target.get("display_name", field_key),
                "description": target.get("description", ""),
                "template_url": target.get("template_url", ""),
            }
            for field_key, target in request.output_targets.items()
        ]

    def _initial_text(self, request: StartAttemptRequest) -> str:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        startup = str(request.startup_prompt or executor.get("startup_prompt") or "").strip()
        if request.startup_capability_key:
            startup = f"${request.startup_capability_key}\n{startup}".strip()
        return startup or f"执行节点：{asset.get('name') or request.node.get('instance_key')}"

    def _context_text(self, request: StartAttemptRequest) -> str:
        asset = cast(dict[str, Any], request.node.get("asset") or {})
        executor = cast(dict[str, Any], asset.get("executor") or {})
        startup = str(request.startup_prompt or executor.get("startup_prompt") or "").strip()
        context = str(executor.get("context_prompt") or "").strip()
        inputs = [self._artifact_input(item) for item in request.bindings]
        outputs = self._output_contract(request)
        collaboration = request.interaction_mode == "COLLABORATION"
        sections = (
            [
                "你正在一个由人工新建的独立协作会话中。等待并响应用户在本会话中的请求。"
                "节点启动提示词在本会话中仅作背景，不是需要独立执行的预设任务。"
                "可用 Skill 与 MCP 均为候选能力：先理解用户意图，再自行选择真正相关的能力；"
                "用户通过 $ 显式指定能力时必须优先遵循。不要仅因某项能力是节点默认值就调用它。"
            ]
            if collaboration
            else []
        )
        if collaboration and startup:
            sections.append(
                "节点预置说明（仅作协作背景，不是需要独立执行或立即答复的用户任务）：\n" + startup
            )
        if context:
            heading = "节点背景上下文（仅作协作参考）" if collaboration else "任务上下文"
            sections.append(f"{heading}：\n{context}")
        if collaboration and request.conversation_history:
            sections.append(
                "这是从既有会话分叉出的独立会话。下列记录是分叉点及其之前的真实对话历史，"
                "按给定顺序视为本会话已经发生的上下文；不要声称看不到这些消息，也不要重复回答"
                "最后一条历史消息。等待用户的新消息后，从该上下文继续。\n"
                + json.dumps(request.conversation_history, ensure_ascii=False)
            )
        if collaboration and request.delegation_enabled:
            sections.append(
                "当任务可拆成互相独立的工作时，你可以让平台自动创建子智能体并行处理。"
                "需要委派时，本轮不要输出普通答复，只调用 finish 并把 message 严格写成以下 JSON，"
                "tasks 最多 4 项；title 是短标题，instruction 必须包含完整、独立、可执行的要求：\n"
                '{"flowweave":{"action":"delegate","tasks":['
                '{"title":"检查后端","instruction":"独立检查后端实现并给出结论"}'
                "]}}\n"
                "平台会在所有子智能体结束后把结构化结果送回本会话，届时你必须综合结果继续回答。"
                "不要为简单任务委派，也不要输出上述控制 JSON 的解释或 Markdown 代码块。"
            )
        elif collaboration:
            sections.append(
                "你是由父智能体创建的子智能体。请只完成收到的独立任务；不得继续委派其他智能体。"
            )
        if request.node_workspace_ref:
            resource_lines = [
                f"节点持久工作目录：{request.node_workspace_ref}",
                f"文本与附件目录：{request.node_workspace_ref}/files",
                f"代码仓库目录：{request.node_workspace_ref}/repositories",
            ]
            if request.skills:
                resource_lines.append("可用 Skills：")
                for skill in request.skills:
                    detail = f"- {skill.name}: {skill.source}（脚本目录 {skill.workspace_path}）"
                    if skill.dependency_runtime_path:
                        detail += (
                            f"；依赖运行器 {skill.dependency_runtime_path}/python 与 "
                            f"{skill.dependency_runtime_path}/node"
                        )
                    resource_lines.append(detail)
                resource_lines.append(
                    "只有上面明确列出的 Skill 已绑定到本 Runtime。若某个 Skill 文档引用了"
                    "未列出的兄弟 Skill（例如 ../other-skill/SKILL.md），该依赖当前不可用；"
                    "不要猜测或反复探测不存在的路径，应使用当前 Skill 已提供的说明、references、"
                    "脚本或 CLI --help 继续，确实无法执行时再明确报告缺失依赖。"
                )
            if request.mcp_servers:
                resource_lines.append("可用 MCP Servers：")
                resource_lines.extend(
                    f"- {server.name}: {server.workspace_path}" for server in request.mcp_servers
                )
            resource_lines.append(
                "这些 Skill 与 MCP 是可选能力；根据用户当前消息动态选择。"
                "用户显式选择时必须优先调用；Skill 附带脚本可直接从上述目录执行。"
                if collaboration
                else "用户在消息中显式选择 Skill 或 MCP 时，必须优先调用所选能力；"
                "Skill 附带脚本可直接从上述目录执行。"
            )
            sections.append("运行资源：\n" + "\n".join(resource_lines))
        input_heading = "当前 Attempt 输入（协作参考）" if collaboration else "流程输入"
        rendered_inputs = json.dumps(inputs, ensure_ascii=False, default=str)
        sections.append(f"{input_heading}：\n{rendered_inputs}")
        if inputs and not collaboration:
            sections.append(
                "输入项中的 uri 是本次运行实际读取的飞书文档；非空的 template_url 是节点定义时"
                "指定的可选格式与结构参考。请以实际输入内容为事实来源；有模板时参考其组织方式，"
                "没有模板时按 description 和任务要求处理。不得修改输入文档或输入模板。"
            )
        if outputs:
            sections.append(
                "平台不会持有或注入飞书账号凭据。请使用本终端环境内已登录的 lark-cli，"
                "在 root_url 下按 run_name/标题创建并编辑下列输出文档；有 template_url 时复制模板，"
                "否则创建空白文档。完成后调用 finish，并把 message 严格写成 JSON："
                '{"outputs":{"字段标识":{"artifact_type":"URL","uri":"https://..."}}}'
                "。不得把 token、cookie 或本地凭据文件写入消息。\n"
                + json.dumps(outputs, ensure_ascii=False)
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
            "llm": self._llm_payload(provider),
            "tools": [
                {"name": "terminal", "params": {}},
                {"name": "file_editor", "params": {}},
                {"name": "task_tracker", "params": {}},
            ],
        }
        agent["agent_context"] = {
            "skills": skills,
            "system_message_suffix": self._context_text(request),
        }
        if request.mcp_servers:
            agent["mcp_config"] = {server.name: server.config for server in request.mcp_servers}
        payload: dict[str, Any] = {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": self._workspace_path(request.workspace_ref),
            },
            "max_iterations": int(executor.get("max_iterations") or 100),
            "agent": agent,
        }
        if request.hook_config:
            payload["hook_config"] = request.hook_config
        if run:
            payload["initial_message"] = {
                "role": "user",
                "content": [{"type": "text", "text": self._initial_text(request)}],
                "run": True,
            }
        if request.environment_image and not (
            request.runtime_sandbox_id
            and request.runtime_resource_name
            and request.runtime_base_url
        ):
            raise DomainError(
                "RUNTIME_SANDBOX_REQUIRED",
                "A published environment Runtime must be allocated by the sandbox control plane",
                500,
            )
        target_base_url = request.runtime_base_url or self.base_url
        created = self._request(
            "POST",
            "/api/conversations",
            base_url=target_base_url,
            session_api_key=self._session_key_for_resource(request.runtime_resource_name or None),
            json=payload,
        )
        conversation_id = str(created.get("id") or "")
        if not conversation_id:
            raise DomainError("RUNTIME_PROTOCOL_ERROR", "Missing conversation id", 502)
        self._contracts[conversation_id] = self._output_contract(request)
        cursor_value = created.get("leaf_event_id") or created.get("last_user_message_id")
        cursor = str(cursor_value) if cursor_value else None
        job_id = (
            f"{'env-exec' if run else 'env-chat'}:{request.runtime_resource_name}"
            if request.runtime_resource_name
            else conversation_id
        )
        return RuntimeHandle(job_id=job_id, conversation_id=conversation_id, cursor=cursor)

    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=False)

    def start(self, request: StartAttemptRequest) -> RuntimeHandle:
        return self._create(request, run=True)

    @staticmethod
    def _text_content(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            item = cast(dict[str, object], value)
            text = item.get("text")
            if isinstance(text, str):
                return text
            return OpenHandsRuntime._text_content(item.get("content"))
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
                return cls._text_content(value)
        if kind == "ObservationEvent":
            observation = item.get("observation")
            if isinstance(observation, dict):
                observation_item = cast(dict[str, object], observation)
                value = observation_item.get("content") or observation_item.get("message")
                return cls._text_content(value)
            value = item.get("content") or item.get("message")
            return cls._text_content(value)
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
            observation = item.get("observation")
            observation_kind = (
                str(cast(dict[str, object], observation).get("kind") or "")
                if isinstance(observation, dict)
                else ""
            )
            if observation_kind == "FinishObservation":
                return "COMPLETED"
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
        self,
        conversation_id: str,
        cursor: str | None,
        *,
        base_url: str | None = None,
        session_api_key: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        items: list[dict[str, Any]] = []
        page_id = cursor
        first_page = True
        while True:
            params: dict[str, Any] = {"limit": 100, "sort_order": "TIMESTAMP"}
            if page_id:
                params["page_id"] = page_id
            data = self._request(
                "GET",
                f"/api/conversations/{conversation_id}/events/search",
                base_url=base_url,
                session_api_key=session_api_key,
                params=params,
            )
            raw_items: object = data.get("items", [])
            page_items = (
                [
                    cast(dict[str, Any], item)
                    for item in cast(list[object], raw_items)
                    if isinstance(item, dict)
                ]
                if isinstance(raw_items, list)
                else []
            )
            if first_page and cursor:
                anchor_index = next(
                    (
                        index
                        for index, item in enumerate(page_items)
                        if str(item.get("id") or "") == cursor
                    ),
                    None,
                )
                if anchor_index is None:
                    return [], cursor
                page_items = page_items[anchor_index + 1 :]
            items.extend(page_items)
            next_page_id = str(data.get("next_page_id") or "") or None
            if not next_page_id or next_page_id == page_id:
                break
            page_id = next_page_id
            first_page = False
        # OpenHands treats page_id as an inclusive event anchor, not as an
        # opaque "start after this event" cursor.  The adapter persists the
        # last projected event id as its cursor, so exposing the anchor again
        # would replay a previous FinishAction as the result of the next human
        # message.  Only events created after the persisted anchor belong to
        # the current poll.  If OpenHands cannot find the anchor it starts at
        # the beginning of the log; reject that fallback rather than replaying
        # arbitrary history into the current turn.
        next_cursor = str(items[-1].get("id")) if items and items[-1].get("id") else cursor
        return items, next_cursor

    def _outputs(self, conversation_id: str, text: str) -> dict[str, tuple[str, str]]:
        expected = {item["field_key"] for item in self._contracts.get(conversation_id, [])}
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else ""
        try:
            value: object = json.loads(candidate)
        except ValueError:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                value = json.loads(candidate[start : end + 1])
            except ValueError:
                return {}
        if not isinstance(value, dict):
            return {}
        raw_outputs = cast(dict[str, object], value).get("outputs")
        if not isinstance(raw_outputs, dict):
            return {}
        outputs: dict[str, tuple[str, str]] = {}
        for field_key, raw in cast(dict[object, object], raw_outputs).items():
            key = str(field_key)
            if key not in expected:
                continue
            if isinstance(raw, str):
                uri = raw
            elif isinstance(raw, dict):
                item = cast(dict[object, object], raw)
                uri = str(item.get("uri") or item.get("url") or "")
            else:
                continue
            parsed = urlparse(uri)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                host == suffix or host.endswith(f".{suffix}")
                for suffix in ("feishu.cn", "larksuite.com", "larkoffice.com")
            ):
                continue
            outputs[key] = ("URL", uri)
        return outputs

    def _result_from_events(
        self,
        conversation_id: str,
        items: list[dict[str, Any]],
        cursor: str | None,
        *,
        assistant_message_is_final: bool = False,
    ) -> RuntimeResult | None:
        for item in reversed(items):
            if self._event_type(item) == "COMPLETED":
                text = self._event_text(item)
                return RuntimeResult(
                    status="COMPLETED",
                    outputs=self._outputs(conversation_id, text),
                    final_message=text,
                    cursor=cursor,
                )
            if self._event_type(item) == "ERROR":
                return RuntimeResult(
                    status="FAILED",
                    error=self._event_text(item) or "OpenHands failed",
                    cursor=cursor,
                )
            if assistant_message_is_final and str(item.get("kind") or "") == "MessageEvent":
                message = item.get("llm_message")
                role = (
                    str(cast(dict[str, object], message).get("role") or "").lower()
                    if isinstance(message, dict)
                    else ""
                )
                if role == "assistant" or str(item.get("source") or "").lower() == "agent":
                    text = self._event_text(item)
                    if text:
                        return RuntimeResult(
                            status="COMPLETED",
                            outputs=self._outputs(conversation_id, text),
                            final_message=text,
                            cursor=cursor,
                        )
        return None

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        items, cursor = self._events(
            handle.conversation_id,
            handle.cursor,
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
        )
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

    async def stream_events(self, handle: RuntimeHandle) -> AsyncIterator[dict[str, Any]]:
        """Relay transient visible-text deltas without persisting model reasoning."""

        route = self._environment_route(handle.job_id)
        if route is not None and controller_is_remote(self.settings):
            if not handle.runtime_resource_id or not handle.runtime_resource_name:
                raise DomainError(
                    "AGENT_STREAM_UNAVAILABLE",
                    "The isolated Runtime stream has no verified sandbox binding",
                    409,
                )
            async for event in DockerControllerClient(self.settings).stream_runtime_events(
                resource_name=handle.runtime_resource_name,
                resource_id=handle.runtime_resource_id,
                conversation_id=handle.conversation_id,
            ):
                visible = self._visible_stream_event(cast(dict[str, object], event))
                if visible is not None:
                    yield visible
            return

        base_url = self._base_url_for_handle(handle)
        websocket_url = (
            f"{base_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/sockets/events/{handle.conversation_id}"
        )
        headers = {"X-Session-API-Key": self._session_key_for_handle(handle)}
        async with connect(
            websocket_url,
            additional_headers=headers,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ) as upstream:
            async for raw in upstream:
                if not isinstance(raw, str):
                    continue
                try:
                    value: object = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                event = cast(dict[str, object], value)
                visible = self._visible_stream_event(event)
                if visible is not None:
                    yield visible

    @staticmethod
    def _visible_stream_event(event: dict[str, object]) -> dict[str, Any] | None:
        """Map an OpenHands frame to the public stream without exposing reasoning."""

        kind = str(event.get("kind") or "")
        if kind == "StreamingDeltaEvent":
            content = event.get("content")
            return (
                {"type": "delta", "content": content}
                if isinstance(content, str) and content
                else None
            )
        if kind != "MessageEvent":
            return None
        raw_message = event.get("llm_message")
        message = cast(dict[str, object], raw_message) if isinstance(raw_message, dict) else {}
        role = str(message.get("role") or "").lower()
        source = str(event.get("source") or "").lower()
        return {"type": "message_complete"} if role == "assistant" or source == "agent" else None

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        base_url = self._base_url_for_handle(handle)
        data = self._request(
            "GET",
            f"/api/conversations/{handle.conversation_id}",
            base_url=base_url,
            session_api_key=self._session_key_for_handle(handle),
        )
        status = str(data.get("execution_status") or "running").lower()
        cursor = str(data.get("leaf_event_id") or handle.cursor or "") or None
        if status == "finished":
            turn_anchor = str(data.get("last_user_message_id") or handle.cursor or "") or None
            items, event_cursor = self._events(
                handle.conversation_id,
                turn_anchor,
                base_url=base_url,
                session_api_key=self._session_key_for_handle(handle),
            )
            result = self._result_from_events(
                handle.conversation_id,
                items,
                event_cursor or cursor,
                assistant_message_is_final=True,
            )
            # A finished status without a completion event after this turn's
            # anchor can occur while event persistence is catching up.  Keep
            # polling instead of completing the turn with an old or empty
            # result.
            return result or RuntimeResult(status="RUNNING", cursor=event_cursor or handle.cursor)
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

    def send_message(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        parts: list[dict[str, Any]] = []
        if content:
            parts.append({"type": "text", "text": content})
        if image_urls:
            parts.append({"type": "image", "image_urls": list(image_urls)})
        created = self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/events",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={
                "role": "user",
                "content": parts,
                "run": True,
            },
        )
        cursor_value = (
            created.get("id")
            or created.get("event_id")
            or created.get("last_user_message_id")
            or created.get("leaf_event_id")
        )
        if not cursor_value:
            state = self._request(
                "GET",
                f"/api/conversations/{handle.conversation_id}",
                base_url=self._base_url_for_handle(handle),
                session_api_key=self._session_key_for_handle(handle),
            )
            cursor_value = state.get("last_user_message_id") or handle.cursor
        cursor = str(cursor_value) if cursor_value else None
        return RuntimeResult(status="RUNNING", cursor=cursor)

    def switch_model(self, handle: RuntimeHandle, provider: RuntimeProvider) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/switch_llm",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={"llm": self._llm_payload(provider)},
        )

    def interrupt(self, handle: RuntimeHandle) -> None:
        self._request(
            "POST",
            f"/api/conversations/{handle.conversation_id}/interrupt",
            base_url=self._base_url_for_handle(handle),
            session_api_key=self._session_key_for_handle(handle),
            json={},
        )

    def resume(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        self.interrupt(handle)
        return self.send_message(handle, content, image_urls)

    def cancel(self, handle: RuntimeHandle) -> None:
        path = f"/api/conversations/{handle.conversation_id}"
        base_url = self._base_url_for_handle(handle)
        route = self._environment_route(handle.job_id)
        try:
            interrupted = self._request(
                "POST",
                f"{path}/interrupt",
                missing_ok=True,
                base_url=base_url,
                session_api_key=self._session_key_for_handle(handle),
                json={},
            )
            if interrupted.get("_flowweave_missing"):
                return
            for poll_no in range(10):
                data = self._request(
                    "GET",
                    path,
                    missing_ok=True,
                    base_url=base_url,
                    session_api_key=self._session_key_for_handle(handle),
                )
                if data.get("_flowweave_missing"):
                    return
                status = str(data.get("execution_status") or "").lower()
                if status not in {"starting", "running", "executing", "stopping"}:
                    return
                if poll_no < 9:
                    time.sleep(0.1)
            raise DomainError(
                "EXECUTOR_CANCEL_UNCONFIRMED",
                "OpenHands accepted the interrupt but the Agent is still running",
                503,
                {"conversation_id": handle.conversation_id},
            )
        except DomainError:
            # Managed Runtime deletion is authoritative and handled by the
            # sandbox control plane. The HTTP endpoint may already be gone.
            if route is None:
                raise
