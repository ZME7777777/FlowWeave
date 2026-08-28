from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import urlparse

from flowweave.runtime.base import (
    RuntimeAskAgentResult,
    RuntimeConversationIdentity,
    RuntimeEventBatch,
    RuntimeForkResult,
    RuntimeHandle,
    RuntimeMCPOAuthCallbackRequest,
    RuntimeMCPOAuthJobRequest,
    RuntimeMCPOAuthStartRequest,
    RuntimeMCPOAuthStatus,
    RuntimeMCPProbeRequest,
    RuntimeMCPProbeResult,
    RuntimePendingConfirmation,
    RuntimePluginValidationRequest,
    RuntimePluginValidationResult,
    RuntimeProvider,
    RuntimeResult,
    RuntimeWakeup,
    RuntimeWorkspaceEntry,
    RuntimeWorkspaceFile,
    RuntimeWorkspaceSnapshot,
    StartAttemptRequest,
)
from flowweave.shared.errors import DomainError


class MockRuntime:
    """Deterministic adapter used by tests and local product demos."""

    def __init__(self) -> None:
        self._results: dict[str, RuntimeResult] = {}

    def probe_mcp(self, request: RuntimeMCPProbeRequest) -> RuntimeMCPProbeResult:
        del request
        return RuntimeMCPProbeResult(ok=True, tools=("mock_read",))

    def validate_plugin(
        self, request: RuntimePluginValidationRequest
    ) -> RuntimePluginValidationResult:
        return RuntimePluginValidationResult(
            plugin_name=request.plugin.name,
            plugin_version="mock",
            skill_count=0,
            command_count=0,
            agent_count=0,
            mcp_server_count=0,
            has_hooks=False,
        )

    def start_mcp_oauth(self, request: RuntimeMCPOAuthStartRequest) -> RuntimeMCPOAuthStatus:
        del request
        return RuntimeMCPOAuthStatus(
            ok=True,
            status="authorizing",
            job_id="mock-mcp-oauth-job",
            authorization_url="https://identity.example.test/authorize?state=redacted",
        )

    def read_mcp_oauth(self, request: RuntimeMCPOAuthJobRequest) -> RuntimeMCPOAuthStatus:
        return RuntimeMCPOAuthStatus(
            ok=True,
            status="authorizing",
            job_id=request.job_id,
            callback_ready=True,
        )

    def submit_mcp_oauth_callback(
        self, request: RuntimeMCPOAuthCallbackRequest
    ) -> RuntimeMCPOAuthStatus:
        return RuntimeMCPOAuthStatus(
            ok=True,
            status="succeeded",
            job_id=request.job_id,
            tools=("mock_read",),
            oauth_state={"tokens": {"access_token": "mock-secret"}},
        )

    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle:
        handle = RuntimeHandle(
            job_id=f"mock-job-{request.attempt_id}",
            conversation_id=request.conversation_id or f"mock-conversation-{request.attempt_id}",
            cursor="1",
        )
        self._results[handle.job_id] = RuntimeResult(status="RUNNING", cursor="1")
        return handle

    def rename_conversation(self, handle: RuntimeHandle, title: str) -> None:
        del handle, title

    def delete_conversation(self, handle: RuntimeHandle) -> None:
        self._results.pop(handle.job_id, None)

    def start(self, request: StartAttemptRequest) -> RuntimeHandle:
        handle = RuntimeHandle(
            job_id=f"mock-job-{request.attempt_id}",
            conversation_id=f"mock-conversation-{request.attempt_id}",
            cursor="1",
        )
        if request.node.get("config_override", {}).get("mock_human_required"):
            result = RuntimeResult(
                status="HUMAN_INPUT_REQUIRED",
                human_question="请补充执行所需信息",
                cursor="1",
            )
        else:
            outputs: dict[str, tuple[str, str]] = {}
            for field in request.node["asset"].get("outputs", []):
                field_key = str(field["field_key"])
                data_type = str(field["data_type"])
                if data_type == "URL":
                    target = request.output_targets.get(field_key, {})
                    root = urlparse(str(target.get("root_url") or ""))
                    host = root.netloc or "example.feishu.cn"
                    content = f"https://{host}/docx/mock-docx-{field_key}"
                else:
                    content = (
                        f"Mock output for "
                        f"{request.node.get('alias') or request.node['asset']['name']}"
                        f" · {field_key}"
                    )
                outputs[field_key] = (data_type, content)
            result = RuntimeResult(status="COMPLETED", outputs=outputs, cursor="2")
        self._results[handle.job_id] = result
        return handle

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        return RuntimeEventBatch(cursor=handle.cursor)

    def reload_conversation(
        self,
        handle: RuntimeHandle,
        *,
        expected: RuntimeConversationIdentity | None = None,
    ) -> RuntimeConversationIdentity:
        identity = RuntimeConversationIdentity(
            conversation_id=handle.conversation_id,
            workspace_working_dir="/runtime/workspace/project",
            persistence_dir=(
                f"/runtime/state/conversations/{handle.conversation_id.replace('-', '')}"
                if handle.conversation_id.count("-") == 4
                else "/mock/conversations"
            ),
            event_id=handle.cursor,
            parent_id=None,
            action_id=None,
            tool_call_id=None,
        )
        if expected is not None and identity != expected:
            raise DomainError(
                "RUNTIME_RELOAD_IDENTITY_MISMATCH",
                "The mock Conversation identity did not survive reload",
                409,
            )
        return identity

    async def stream_events(self, handle: RuntimeHandle) -> AsyncIterator[dict[str, Any]]:
        del handle
        if False:
            yield {}

    def wait_for_wakeup(
        self,
        handle: RuntimeHandle,
        *,
        channel: Literal["CONVERSATION", "BASH"],
        timeout_seconds: float,
        cursor: str | None = None,
    ) -> RuntimeWakeup:
        del handle, timeout_seconds
        return RuntimeWakeup(
            channel=channel,
            cursor=cursor,
        )

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        return self._results.get(handle.job_id, RuntimeResult(status="FAILED", error="UNKNOWN_JOB"))

    def read_active_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        return self.read_events(handle)

    def switch_model(self, handle: RuntimeHandle, provider: RuntimeProvider) -> None:
        del handle, provider

    def upload_workspace_file(
        self,
        handle: RuntimeHandle,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        attachment_owner_id: str | None = None,
    ) -> str:
        del filename, content_type, content
        return f"/runtime/workspace/project/uploads/{attachment_owner_id or handle.conversation_id}-{uuid4().hex}"

    def workspace_snapshot(self, handle: RuntimeHandle, path: str) -> RuntimeWorkspaceSnapshot:
        del handle
        return RuntimeWorkspaceSnapshot(
            entries=(
                RuntimeWorkspaceEntry(path=f"{path.rstrip('/')}/README.md", kind="file", size=128),
                RuntimeWorkspaceEntry(path=f"{path.rstrip('/')}/src", kind="directory"),
            ),
            repositories=(),
        )

    def download_workspace_file(self, handle: RuntimeHandle, path: str) -> RuntimeWorkspaceFile:
        del handle
        return RuntimeWorkspaceFile(path.rsplit("/", 1)[-1], "text/plain", b"mock workspace file\n")

    def conversation_context(self, handle: RuntimeHandle) -> dict[str, int | str | None]:
        del handle
        return {
            "used_tokens": None,
            "window_tokens": None,
            "cumulative_tokens": None,
            "provider_id": None,
            "model_name": None,
            "reasoning_effort": None,
        }

    def interrupt(self, handle: RuntimeHandle) -> None:
        del handle

    def can_accept_input(self, handle: RuntimeHandle) -> bool:
        return self._results.get(handle.job_id, RuntimeResult(status="IDLE")).status != "RUNNING"

    def navigate(self, handle: RuntimeHandle, event_id: str | None) -> None:
        del handle, event_id

    def run(self, handle: RuntimeHandle) -> RuntimeResult:
        result = RuntimeResult(status="RUNNING", cursor=handle.cursor)
        self._results[handle.job_id] = result
        return result

    def get_pending_confirmation(self, handle: RuntimeHandle) -> RuntimePendingConfirmation | None:
        del handle
        return None

    def respond_to_confirmation(
        self,
        handle: RuntimeHandle,
        expected_pending_digest: str,
        accept: bool,
        reason: str,
    ) -> RuntimeResult:
        del expected_pending_digest, accept, reason
        result = RuntimeResult(status="RUNNING", cursor=handle.cursor)
        self._results[handle.job_id] = result
        return result

    def condense(self, handle: RuntimeHandle) -> RuntimeResult:
        result = RuntimeResult(status="RUNNING", cursor=handle.cursor)
        self._results[handle.job_id] = result
        return result

    def start_goal(self, handle: RuntimeHandle, objective: str, max_iterations: int) -> None:
        del handle, objective, max_iterations

    def stop_goal(self, handle: RuntimeHandle) -> None:
        del handle

    def resume_goal(self, handle: RuntimeHandle) -> None:
        del handle

    def ask_agent(
        self, handle: RuntimeHandle, question: str, *, timeout_seconds: float
    ) -> RuntimeAskAgentResult:
        del handle, timeout_seconds
        return RuntimeAskAgentResult(response=f"Mock diagnostic: {question}")

    def fork_conversation(
        self,
        handle: RuntimeHandle,
        *,
        target_conversation_id: str,
        title: str,
        from_event_id: str | None,
        expected_source_leaf_event_id: str,
        reset_metrics: bool,
    ) -> RuntimeForkResult:
        del title
        fork_handle = RuntimeHandle(
            job_id=handle.job_id,
            conversation_id=target_conversation_id,
            cursor=from_event_id or expected_source_leaf_event_id,
            runtime_resource_id=handle.runtime_resource_id,
            runtime_resource_name=handle.runtime_resource_name,
        )
        self._results[fork_handle.job_id] = RuntimeResult(
            status="RUNNING", cursor=fork_handle.cursor
        )
        return RuntimeForkResult(
            handle=fork_handle,
            source_conversation_id=handle.conversation_id,
            source_event_id=from_event_id,
            leaf_event_id=fork_handle.cursor,
            reset_metrics=reset_metrics,
        )

    def send_message(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        image_note = f" · {len(image_urls)} image(s)" if image_urls else ""
        result = RuntimeResult(
            status="COMPLETED",
            outputs={"result": ("TEXT", f"Mock response: {content}{image_note}")},
            cursor="3",
        )
        self._results[handle.job_id] = result
        return result

    def resume(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult:
        return self.send_message(handle, content, image_urls)

    def cancel(self, handle: RuntimeHandle) -> None:
        self._results[handle.job_id] = RuntimeResult(status="CANCELLED")
