from __future__ import annotations

from flowweave.modules.gates.application.executor import execute_gate


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


def test_javascript_gate_executes_with_limits(db_session_factory):
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
    assert result.decision == "PASS"


def test_javascript_gate_interrupts_infinite_loop(db_session_factory):
    with db_session_factory() as db:
        result = execute_gate(db, "JAVASCRIPT", {"code": "while (true) {}"}, {}, 1)
    assert result.decision == "ERROR"
    assert result.error_code == "GATE_TIMEOUT"


def test_invalid_gate_result_is_normalized_to_error(db_session_factory):
    with db_session_factory() as db:
        result = execute_gate(db, "JAVASCRIPT", {"code": "return {decision: 'MAYBE'};"}, {}, 1)
    assert result.decision == "ERROR"
    assert result.error_code == "GATE_RESULT_INVALID"


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
    with db_session_factory() as db:
        provider = ModelProvider(name="gate-provider", base_url="https://models.test/v1")
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
