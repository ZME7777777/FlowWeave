from __future__ import annotations

import json

from flowweave.modules.gates.application.executor import (
    GateExecutionPlan,
    _normalize,
    execute_gate,
    execute_gate_plan,
)
from flowweave.modules.orchestration.application.service import (
    _business_result_contract_review_policies,
    _gate_review_projection,
    _startup_prompt_with_business_result_contract,
)
from flowweave.runtime.base import (
    RuntimeEventBatch,
    RuntimeResult,
    RuntimeUsageSnapshot,
    StartAttemptRequest,
)
from flowweave.runtime.dependencies import runtime_context
from flowweave.runtime.mock import MockRuntime
from flowweave.shared.schemas import GateWrite


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


def test_gate_normalizes_surrounding_decision_whitespace():
    result = _normalize(
        {"decision": "  pass\n", "summary": "checked", "reasons": [], "evidence": [], "details": {}}
    )

    assert result.decision == "PASS"


def test_system_gate_messages_are_chinese_without_changing_the_result_protocol():
    invalid_result = _normalize([])
    fallback_summary = _normalize(
        {"decision": "PASS", "reasons": [], "evidence": [], "details": {}}
    )
    unsupported_type = execute_gate_plan(GateExecutionPlan("JAVASCRIPT", {}, 1), {})

    assert invalid_result.summary == "门禁结果必须是 JSON 对象"
    assert fallback_summary.summary == "门禁返回判定：PASS"
    assert unsupported_type.summary == "不支持的门禁类型：JAVASCRIPT"
    assert invalid_result.error_code == "GATE_RESULT_INVALID"
    assert unsupported_type.error_code == "GATE_CONFIG_INVALID"


def test_platform_output_contract_only_checks_artifacts_types_and_port_mappings():
    """The platform check must not make a semantic document-quality judgement."""

    result = execute_gate_plan(
        GateExecutionPlan("PLATFORM_OUTPUT_CONTRACT", {}, 1),
        {
            "node": {
                "outputs": [
                    {"field_key": "task_table", "data_type": "FILE"},
                    {"field_key": "report", "data_type": "FILE"},
                ]
            },
            # Deliberately non-tabular text: content quality belongs to an
            # author-configured END gate, not this deterministic platform check.
            "outputs": [
                {
                    "id": "artifact-table",
                    "field_key": "task_table",
                    "artifact_type": "FILE",
                    "review_preview": {"kind": "TEXT", "content": "not a table"},
                },
                {
                    "id": "artifact-report",
                    "field_key": "report",
                    "artifact_type": "FILE",
                },
            ],
            "downstream_consumers": [
                {
                    "instance_key": "N2",
                    "mappings": [
                        {
                            "source_output_key": "task_table",
                            "target_input": {"field_key": "tasks", "data_type": "FILE"},
                        },
                        {
                            "source_output_key": "report",
                            "target_input": {"field_key": "report", "data_type": "FILE"},
                        },
                    ],
                }
            ],
        },
    )

    assert result.decision == "PASS"
    assert result.details["selected_output_artifact_ids"] == ["artifact-table", "artifact-report"]


def test_platform_output_contract_rejects_an_unbindable_mapping():
    result = execute_gate_plan(
        GateExecutionPlan("PLATFORM_OUTPUT_CONTRACT", {}, 1),
        {
            "node": {"outputs": [{"field_key": "report", "data_type": "FILE"}]},
            "outputs": [{"id": "artifact-report", "field_key": "report", "artifact_type": "FILE"}],
            "downstream_consumers": [
                {
                    "instance_key": "N2",
                    "mappings": [
                        {
                            "source_output_key": "report",
                            "target_input": {"field_key": "report_url", "data_type": "URL"},
                        }
                    ],
                }
            ],
        },
    )

    assert result.decision == "FAIL"
    assert "类型不匹配" in result.reasons[0]


def _business_contract(node_kind: str, output_key: str, paths: list[str]):
    return {
        "business_result_contract": {
            "schema_version": 1,
            "node_kind": node_kind,
            "result_output_keys": [output_key],
            "required_evidence_paths": paths,
        }
    }


def _business_output(output_key: str, result: dict[str, object]):
    return {
        "id": f"artifact-{output_key}",
        "field_key": output_key,
        "artifact_type": "FILE",
        "review_preview": {
            "kind": "TEXT",
            "content": '{"flowweave_business_result": ' + json.dumps(result) + "}",
        },
    }


def test_business_result_contract_rejects_blocked_repair_report():
    result = execute_gate_plan(
        GateExecutionPlan(
            "BUSINESS_RESULT_CONTRACT",
            _business_contract("REPAIR", "repair_summary", ["fingerprints", "repository"]),
            1,
        ),
        {
            "outputs": [
                _business_output(
                    "repair_summary",
                    {
                        "schema_version": 1,
                        "status": "BLOCKED",
                        "node_kind": "REPAIR",
                        "evidence": {"fingerprints": ["fp-1"], "repository": "repo"},
                    },
                )
            ]
        },
    )

    assert result.decision == "FAIL"
    assert "不允许通过" in result.reasons[0]


def test_business_result_contract_rejects_missing_or_malformed_evidence():
    missing = execute_gate_plan(
        GateExecutionPlan(
            "BUSINESS_RESULT_CONTRACT",
            _business_contract("INVESTIGATION", "investigation", ["fingerprints", "trace_ids"]),
            1,
        ),
        {
            "outputs": [
                _business_output(
                    "investigation",
                    {
                        "schema_version": 1,
                        "status": "SUCCESS",
                        "node_kind": "INVESTIGATION",
                        "evidence": {"fingerprints": ["fp-1"]},
                    },
                )
            ]
        },
    )
    malformed = execute_gate_plan(
        GateExecutionPlan(
            "BUSINESS_RESULT_CONTRACT",
            _business_contract("COLLECTION", "collection", ["source", "time_window"]),
            1,
        ),
        {
            "outputs": [
                {
                    "id": "artifact-collection",
                    "field_key": "collection",
                    "artifact_type": "FILE",
                    "review_preview": {"kind": "TEXT", "content": "# a Markdown report"},
                }
            ]
        },
    )

    assert missing.decision == "FAIL"
    assert "trace_ids" in missing.reasons[0]
    assert malformed.decision == "FAIL"
    assert "flowweave_business_result" in malformed.reasons[0]


def test_business_result_contract_accepts_collection_investigation_and_repair():
    cases = [
        ("COLLECTION", "collection", ["source", "time_window"]),
        ("INVESTIGATION", "investigation", ["fingerprints", "trace_ids"]),
        ("REPAIR", "repair", ["fingerprints", "repository", "changed_files", "tests", "delivery"]),
    ]
    for node_kind, output_key, paths in cases:
        evidence = {path: [path] for path in paths}
        result = execute_gate_plan(
            GateExecutionPlan(
                "BUSINESS_RESULT_CONTRACT",
                _business_contract(node_kind, output_key, paths),
                1,
            ),
            {
                "outputs": [
                    _business_output(
                        output_key,
                        {
                            "schema_version": 1,
                            "status": "SUCCESS",
                            "node_kind": node_kind,
                            "evidence": evidence,
                        },
                    )
                ]
            },
        )

        assert result.decision == "PASS"


def test_business_result_contract_allows_explicit_no_findings_only_when_configured():
    output = _business_output(
        "collection",
        {
            "schema_version": 1,
            "status": "NO_FINDINGS",
            "node_kind": "COLLECTION",
            "evidence": {"source": "logs", "time_window": "2026-10-17T00:00Z"},
        },
    )
    default_result = execute_gate_plan(
        GateExecutionPlan(
            "BUSINESS_RESULT_CONTRACT",
            _business_contract("COLLECTION", "collection", ["source", "time_window"]),
            1,
        ),
        {"outputs": [output]},
    )
    configured = _business_contract("COLLECTION", "collection", ["source", "time_window"])
    configured["business_result_contract"]["allowed_statuses"] = ["SUCCESS", "NO_FINDINGS"]
    no_findings_result = execute_gate_plan(
        GateExecutionPlan("BUSINESS_RESULT_CONTRACT", configured, 1), {"outputs": [output]}
    )

    assert default_result.decision == "FAIL"
    assert no_findings_result.decision == "PASS"


def test_business_result_contract_never_allows_blocked_or_repair_no_findings():
    blocked = _business_contract("REPAIR", "repair", ["fingerprints"])
    blocked["business_result_contract"]["allowed_statuses"] = ["SUCCESS", "BLOCKED"]
    repair_no_findings = _business_contract("REPAIR", "repair", ["fingerprints"])
    repair_no_findings["business_result_contract"]["allowed_statuses"] = ["NO_FINDINGS"]
    base = {
        "stage": "END",
        "position": 0,
        "gate_type": "PROMPT",
        "agent_preset": {"model_provider_id": "provider", "model_name": "model"},
        "config": {"prompt": "审查"},
    }

    assert (
        execute_gate_plan(GateExecutionPlan("BUSINESS_RESULT_CONTRACT", blocked, 1), {}).decision
        == "ERROR"
    )
    assert (
        execute_gate_plan(
            GateExecutionPlan("BUSINESS_RESULT_CONTRACT", repair_no_findings, 1), {}
        ).decision
        == "ERROR"
    )
    for contract in (blocked, repair_no_findings):
        try:
            GateWrite.model_validate({**base, "config": {"prompt": "审查", **contract}})
        except ValueError:
            continue
        raise AssertionError("invalid business status configuration must be rejected")


def test_business_result_contract_is_injected_into_the_frozen_execution_prompt_once():
    gates = [
        {
            "config": _business_contract("REPAIR", "repair", ["fingerprints", "tests"]),
        }
    ]

    prompt = _startup_prompt_with_business_result_contract("修复异常", gates)

    assert prompt is not None
    assert "FLOWWEAVE_BUSINESS_RESULT_CONTRACT_V1" in prompt
    assert '"node_kind": "REPAIR"' in prompt
    assert _startup_prompt_with_business_result_contract(prompt, gates) == prompt


def test_business_result_contract_runs_between_platform_and_author_end_gate():
    author_policy = {
        "id": "author-end-gate",
        "stage": "END",
        "position": 0,
        "gate_type": "PROMPT",
        "config": _business_contract("REPAIR", "repair", ["fingerprints", "tests"]),
    }

    derived = _business_result_contract_review_policies([author_policy])

    assert derived == [
        {
            "id": "__business_result_contract__:author-end-gate",
            "stage": "END",
            "position": -9000,
            "gate_type": "BUSINESS_RESULT_CONTRACT",
            "enabled": True,
            "timeout_seconds": 1,
            "config": {
                "system_owned": True,
                "source_gate_policy_id": "author-end-gate",
                "business_result_contract": author_policy["config"]["business_result_contract"],
            },
        }
    ]


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
        sidecar_question=("You are an isolated workflow gate Agent. Return only a JSON object."),
    )
    with runtime_context(runtime):
        result = execute_gate_plan(plan, {})

    assert result.decision == "PASS"
    assert runtime.created == ["gate-sidecar-conversation"]
    assert runtime.deleted == ["gate-sidecar-conversation"]


def test_agent_sidecar_gate_can_retain_created_conversation_for_audit():
    class TrackingRuntime(MockRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []
            self.last_handle = None

        def create_conversation(self, request):
            self.last_handle = super().create_conversation(request)
            return self.last_handle

        def delete_conversation(self, handle):
            self.deleted.append(handle.conversation_id)
            return super().delete_conversation(handle)

    runtime = TrackingRuntime()
    request = StartAttemptRequest(
        attempt_id="gate-sidecar-binding",
        execution_key="gate-sidecar:audit",
        node={},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id="gate-sidecar-audit-conversation",
        interaction_mode="COLLABORATION",
    )
    plan = GateExecutionPlan(
        "PROMPT",
        {"prompt": "check"},
        2,
        sidecar_request=request,
        sidecar_question="Return one JSON gate result.",
        retain_sidecar=True,
    )

    with runtime_context(runtime):
        result = execute_gate_plan(plan, {})

    assert result.sidecar_available is True
    assert runtime.deleted == []
    assert runtime.last_handle is not None
    events = runtime.read_events(runtime.last_handle)
    assert [event.payload["source"] for event in events.events] == ["user", "agent"]


def test_agent_sidecar_gate_captures_native_usage_when_it_completes():
    class UsageRuntime(MockRuntime):
        def read_active_events(self, handle):
            batch = super().read_active_events(handle)
            return RuntimeEventBatch(
                events=batch.events,
                cursor=batch.cursor,
                result=batch.result,
                usage=(
                    RuntimeUsageSnapshot(
                        usage_id="flowweave:primary",
                        model_name="test-model",
                        accumulated_cost=0.12,
                        prompt_tokens=100,
                        completion_tokens=20,
                        cache_read_tokens=30,
                        cache_write_tokens=4,
                        reasoning_tokens=6,
                        context_window=16000,
                        per_turn_tokens=120,
                    ),
                ),
            )

    request = StartAttemptRequest(
        attempt_id="gate-sidecar-binding",
        execution_key="gate-sidecar:usage",
        node={},
        bindings=[],
        workspace_ref="/runtime/workspace/project",
        conversation_id="gate-sidecar-usage-conversation",
        interaction_mode="COLLABORATION",
    )
    plan = GateExecutionPlan(
        "PROMPT",
        {"prompt": "check"},
        2,
        sidecar_request=request,
        sidecar_question="You are an isolated workflow gate Agent.",
        retain_sidecar=True,
    )

    with runtime_context(UsageRuntime()):
        result = execute_gate_plan(plan, {})

    assert result.sidecar_available is True
    assert len(result.sidecar_usage) == 1
    assert result.sidecar_usage[0].prompt_tokens == 100


def test_gate_review_projection_retains_criteria_and_evidence_not_artifact_contents():
    projection = _gate_review_projection(
        {
            "stage": "END",
            "node": {"instance_key": "repair", "outputs": [{"field_key": "report"}]},
            "input_bindings": [],
            "outputs": [
                {
                    "id": "artifact-1",
                    "field_key": "report",
                    "artifact_type": "FILE",
                    "content_hash": "hash",
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "metadata": {"filename": "report.md"},
                    "inline_content": "secret artifact body",
                    "review_preview": {
                        "kind": "TEXT",
                        "content": "secret artifact body",
                        "truncated": False,
                    },
                }
            ],
            "downstream_consumers": [],
        },
        {"config": {"prompt": "检查报告是否完整", "code": "assert True"}},
    )

    assert projection["criteria"] == {"instructions": "检查报告是否完整", "script": "assert True"}
    candidate = projection["review_context"]["candidate_outputs"][0]
    assert candidate["filename"] == "report.md"
    assert candidate["review_preview"] == {
        "kind": "TEXT",
        "character_count": 20,
        "truncated": False,
    }
    assert "inline_content" not in candidate
    assert "secret artifact body" not in str(projection)


def test_agent_sidecar_gate_accepts_fenced_json_response():
    class FencedRuntime(MockRuntime):
        def send_message(self, handle, content, image_urls=()):
            del content, image_urls
            self._results[handle.job_id] = RuntimeResult(
                status="COMPLETED",
                final_message=(
                    "The gate result is:\n```json\n"
                    '{"decision":"PASS","summary":"checked",'
                    '"reasons":[],"evidence":[],"details":{}}\n```'
                ),
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

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            self.questions.append(content)
            if len(self.questions) == 1:
                answer = '{"decision": "PASS", "summary": invalid}'
            else:
                answer = (
                    '{"decision":"PASS","summary":"checked",'
                    '"reasons":[],"evidence":[],"details":{}}'
                )
            self._results[handle.job_id] = RuntimeResult(status="COMPLETED", final_message=answer)

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


def test_agent_sidecar_gate_retries_once_for_unsupported_decision():
    class CorrectingRuntime(MockRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.questions: list[str] = []

        def send_message(self, handle, content, image_urls=()):
            del image_urls
            self.questions.append(content)
            if len(self.questions) == 1:
                answer = (
                    '{"decision":"INSUFFICIENT_EVIDENCE","summary":"证据不足",'
                    '"reasons":[],"evidence":[],"details":{}}'
                )
            else:
                answer = (
                    '{"decision":"FAIL","summary":"证据不足，未通过",'
                    '"reasons":["缺少可验证证据"],"evidence":[],"details":{}}'
                )
            self._results[handle.job_id] = RuntimeResult(status="COMPLETED", final_message=answer)

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

    assert result.decision == "FAIL"
    assert result.error_code is None
    assert len(runtime.questions) == 2
    assert "INSUFFICIENT_EVIDENCE" in runtime.questions[1]


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
    assert result.summary == "门禁脚本执行失败"
    assert result.error_code == "GATE_ERROR"


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
