from __future__ import annotations

from flowweave.runtime.base import (
    RuntimeEventBatch,
    RuntimeHandle,
    RuntimeResult,
    StartAttemptRequest,
)


class MockRuntime:
    """Deterministic adapter used by tests and local product demos."""

    def __init__(self) -> None:
        self._results: dict[str, RuntimeResult] = {}

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
            outputs = {
                field["field_key"]: (
                    field["data_type"],
                    f"Mock output for {request.node.get('alias') or request.node['asset']['name']}"
                    f" · {field['field_key']}",
                )
                for field in request.node["asset"].get("outputs", [])
            }
            result = RuntimeResult(status="COMPLETED", outputs=outputs, cursor="2")
        self._results[handle.job_id] = result
        return handle

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch:
        return RuntimeEventBatch(cursor=handle.cursor)

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult:
        return self._results.get(handle.job_id, RuntimeResult(status="FAILED", error="UNKNOWN_JOB"))

    def resume(self, handle: RuntimeHandle, content: str) -> RuntimeResult:
        result = RuntimeResult(
            status="COMPLETED",
            outputs={"result": ("TEXT", f"Mock response: {content}")},
            cursor="3",
        )
        self._results[handle.job_id] = result
        return result

    def cancel(self, handle: RuntimeHandle) -> None:
        self._results[handle.job_id] = RuntimeResult(status="CANCELLED")
