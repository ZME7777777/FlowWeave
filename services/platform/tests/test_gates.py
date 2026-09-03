from __future__ import annotations

from flowweave.modules.gates.application.executor import (
    GateExecutionPlan,
    execute_gate,
    execute_gate_plan,
)
from flowweave.runtime.base import RuntimeAskAgentResult, StartAttemptRequest
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime


def test_python_gate_executes_in_restricted_runner(db_session_factory):
    with db_session_factory() as db:
        result = execute_gate(
            db,
            "PYTHON",
            {
                "code": (
                    "result = {'decision': 'PASS' if len(context['artifacts']) == 1 else 'FAIL', "
                    "'summary': 'checked', 'reasons': [], 'evidence': [], 'details': {}}"
                )
            },
            {"artifacts": [{"field_key": "prd"}]},
            2,
        )
    assert result.decision == "PASS"
    assert result.summary == "checked"


def test_agent_sidecar_gate_uses_isolated_conversation_and_json_result():
    class TrackingRuntime(MockRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.created: list[str] = []
            self.deleted: list[str] = []

        def create_conversation(self, request: StartAttemptRequest):
            self.created.append(request.conversation_id or "")
            return super().create_conversation(request)

        def delete_conversation(self, handle):
            self.deleted.append(handle.conversation_id)
            return super().delete_conversation(handle)

    runtime = TrackingRuntime()
    request = StartAttemptRequest(
        attempt_id="gate-sidecar-binding",
        execution_key="gate-sidecar:test",
        node={},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id="gate-sidecar-conversation",
        interaction_mode="COLLABORATION",
    )
    plan = GateExecutionPlan(
        "PROMPT",
        {"prompt": "check"},
        2,
        sidecar_request=request,
        sidecar_question=(
            "You are an isolated workflow gate Agent. Return only a JSON object."
        ),
    )
    with runtime_context(runtime):
        result = execute_gate_plan(plan, {})

    assert result.decision == "PASS"
    assert runtime.created == ["gate-sidecar-conversation"]
    assert runtime.deleted == ["gate-sidecar-conversation"]


def test_agent_sidecar_gate_accepts_fenced_json_response():
    class FencedRuntime(MockRuntime):
        def ask_agent(self, handle, question, *, timeout_seconds):
            del handle, question, timeout_seconds
            return RuntimeAskAgentResult(
                response=(
                    "The gate result is:\n```json\n"
                    '{"decision":"PASS","summary":"checked",'
                    '"reasons":[],"evidence":[],"details":{}}\n```'
                )
            )

    request = StartAttemptRequest(
        attempt_id="gate-sidecar-binding",
        execution_key="gate-sidecar:test",
        node={},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id="gate-sidecar-conversation",
        interaction_mode="COLLABORATION",
    )
    plan = GateExecutionPlan(
        "PROMPT",
        {"prompt": "check"},
        2,
        sidecar_request=request,
        sidecar_question="You are an isolated workflow gate Agent.",
    )

    with runtime_context(FencedRuntime()):
        result = execute_gate_plan(plan, {})

    assert result.decision == "PASS"


def test_agent_sidecar_gate_retries_once_for_malformed_json():
    class CorrectingRuntime(MockRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.questions: list[str] = []

        def ask_agent(self, handle, question, *, timeout_seconds):
            del handle, timeout_seconds
            self.questions.append(question)
            if len(self.questions) == 1:
                return RuntimeAskAgentResult(
                    response='{"decision": "PASS", "summary": invalid}'
                )
            return RuntimeAskAgentResult(
                response=(
                    '{"decision":"PASS","summary":"checked",'
                    '"reasons":[],"evidence":[],"details":{}}'
                )
            )

    runtime = CorrectingRuntime()
    request = StartAttemptRequest(
        attempt_id="gate-sidecar-binding",
        execution_key="gate-sidecar:test",
        node={},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id="gate-sidecar-conversation",
        interaction_mode="COLLABORATION",
    )
    plan = GateExecutionPlan(
        "PROMPT",
        {"prompt": "check"},
        2,
        sidecar_request=request,
        sidecar_question="You are an isolated workflow gate Agent.",
    )

    with runtime_context(runtime):
        result = execute_gate_plan(plan, {})

    assert result.decision == "PASS"
    assert len(runtime.questions) == 2


def test_python_gate_rejects_imports_and_host_access(db_session_factory):
    with db_session_factory() as db:
        result = execute_gate(
            db,
            "PYTHON",
            {"code": "import os\nresult = {'decision': 'PASS'}"},
            {},
            2,
        )
    assert result.decision == "ERROR"
    assert "Import" in result.summary or "syntax" in result.summary


def test_javascript_gate_is_rejected(db_session_factory):
    with db_session_factory() as db:
        result = execute_gate(
            db,
            "JAVASCRIPT",
            {
                "code": (
                    "return {decision: context.ready ? 'PASS' : 'FAIL', summary: 'checked', "
                    "reasons: [], evidence: [], details: {}};"
                )
            },
            {"ready": True},
            1,
        )
    assert result.decision == "ERROR"
    assert result.error_code == "GATE_CONFIG_INVALID"


def test_prompt_gate_calls_openai_compatible_provider(monkeypatch, db_session_factory):
    import json

    from flowweave.modules.gates.application import executor
    from flowweave.shared.models import ModelProvider, ProviderModel

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "PASS",
                                    "summary": "模型判定通过",
                                    "reasons": [],
                                    "evidence": [],
                                    "details": {},
                                }
                            )
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(executor.httpx, "Client", Client)
    monkeypatch.setattr(
        "flowweave.modules.model_providers.application.service.provider_auth_headers",
        lambda _provider: {"Authorization": "Bearer test-key"},
    )
    with db_session_factory() as db:
        provider = ModelProvider(
            name="gate-provider",
            base_url="https://models.test/v1",
            encrypted_api_key=b"configured-for-test",
        )
        db.add(provider)
        db.flush()
        db.add(
            ProviderModel(
                provider_id=provider.id,
                model_name="gate-model",
                enabled=True,
                is_default=True,
            )
        )
        db.commit()
        result = execute_gate(
            db,
            "PROMPT",
            {"model_provider_id": provider.id, "prompt": "检查输入是否完整"},
            {"artifacts": [{"field_key": "prd"}]},
            2,
        )

    assert result.decision == "PASS"
    assert result.summary == "模型判定通过"
    assert captured["url"] == "https://models.test/v1/chat/completions"
    assert captured["payload"]["model"] == "gate-model"
    assert "prd" in captured["payload"]["messages"][1]["content"]


def test_prompt_gate_calls_connected_codex_oauth_provider(monkeypatch, db_session_factory):
    import json

    from flowweave.modules.gates.application import executor
    from flowweave.modules.model_providers.application.service import CodexRuntimeCredentials
    from flowweave.shared.models import ModelProvider, ProviderModel

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield "data: " + json.dumps(
                {
                    "type": "response.output_text.delta",
                    "delta": json.dumps(
                        {
                            "decision": "PASS",
                            "summary": "OAuth 判定通过",
                            "reasons": [],
                            "evidence": [],
                            "details": {},
                        }
                    ),
                }
            )
            yield "data: [DONE]"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, *, headers, json):
            captured.update(method=method, url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(executor.httpx, "Client", Client)
    monkeypatch.setattr(
        "flowweave.modules.model_providers.application.service.codex_runtime_credentials",
        lambda _db, _provider_id: CodexRuntimeCredentials("oauth-token", "account-1"),
    )
    with db_session_factory() as db:
        provider = ModelProvider(
            name="codex-gate-provider",
            base_url="https://unused.test/v1",
            auth_type="CODEX_OAUTH",
            encrypted_oauth_refresh_token=b"connected",
        )
        db.add(provider)
        db.flush()
        db.add(
            ProviderModel(
                provider_id=provider.id, model_name="gpt-5.4", enabled=True, is_default=True
            )
        )
        db.commit()
        result = execute_gate(
            db, "PROMPT", {"model_provider_id": provider.id, "prompt": "检查输入是否完整"}, {}, 2
        )

    assert result.decision == "PASS"
    assert result.summary == "OAuth 判定通过"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/codex/responses")
    assert captured["headers"]["Authorization"] == "Bearer oauth-token"
    assert captured["payload"]["model"] == "gpt-5.4"
