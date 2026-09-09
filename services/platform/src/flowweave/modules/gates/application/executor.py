from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx
from sqlalchemy.orm import Session

from flowweave.modules.model_providers.public import (
    PromptProviderSnapshot,
    prompt_provider_snapshot,
)
from flowweave.runtime.base import RuntimeUsageSnapshot, StartAttemptRequest
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.application.sandbox import SandboxLanguage
from flowweave.shared.sandbox import get_sandbox

DECISIONS = {"PASS", "FAIL", "ERROR"}


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: str
    summary: str
    reasons: list[str]
    evidence: list[dict[str, Any]]
    details: dict[str, Any]
    log_excerpt: str = ""
    error_code: str | None = None
    # Internal execution fact. It is deliberately excluded from ``as_dict`` so
    # the public gate-result contract remains the Agent's decision only.
    sidecar_available: bool = False
    # Captured from the retained native Conversation immediately after the
    # review turn.  FlowWeave persists this projection with the GateEvaluation;
    # it is not a second usage source and is never exposed in the gate payload.
    sidecar_usage: tuple[RuntimeUsageSnapshot, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision": self.decision,
            "summary": self.summary,
            "reasons": self.reasons,
            "evidence": self.evidence,
            "details": self.details,
        }


def _error(message: str, *, log: str = "", code: str = "GATE_ERROR") -> GateResult:
    return GateResult("ERROR", message, [message], [], {}, log[:4000], code)


def _normalize(value: object) -> GateResult:
    if not isinstance(value, dict):
        return _error("Gate result must be a JSON object", code="GATE_RESULT_INVALID")
    mapping = cast(dict[str, object], value)
    # Models occasionally preserve harmless surrounding whitespace despite the
    # JSON contract. Normalize that transport detail, while retaining a strict
    # closed set of actual decisions.
    decision = str(mapping.get("decision", "ERROR")).strip().upper()
    if decision not in DECISIONS:
        return _error(
            "Gate decision must be PASS, FAIL, or ERROR",
            log=f"unsupported gate decision={decision[:80]!r}",
            code="GATE_RESULT_INVALID",
        )
    summary = str(mapping.get("summary") or f"Gate returned {decision}")[:2000]
    reasons_raw = mapping.get("reasons", [])
    evidence_raw = mapping.get("evidence", [])
    details_raw = mapping.get("details", {})
    if (
        not isinstance(reasons_raw, list)
        or not isinstance(evidence_raw, list)
        or not isinstance(details_raw, dict)
    ):
        return _error("Gate result fields have invalid types", code="GATE_RESULT_INVALID")
    reasons = [str(item)[:1000] for item in cast(list[object], reasons_raw)]
    evidence = [
        cast(dict[str, Any], item)
        for item in cast(list[object], evidence_raw)
        if isinstance(item, dict)
    ]
    details = cast(dict[str, Any], details_raw)
    return GateResult(
        decision,
        summary,
        reasons,
        evidence[:100],
        details,
        error_code="GATE_ERROR" if decision == "ERROR" else None,
    )


def _decode_gate_response(answer: str) -> object:
    """Decode the one JSON result emitted by an isolated Gate Agent.

    ``ask_agent`` returns the model's rendered text rather than a structured
    response-format payload.  The prompt requires a bare JSON object, but a
    compliant result may still be enclosed in a Markdown fence or have a short
    presentation prefix.  Decode a complete object from the first few JSON
    object boundaries and only accept one that carries the gate decision; this
    never attempts to repair malformed JSON.
    """

    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None
    scanned = 0
    for offset, character in enumerate(answer):
        if character != "{":
            continue
        scanned += 1
        if scanned > 32:
            break
        try:
            value, _ = decoder.raw_decode(answer, offset)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(value, dict) and "decision" in value:
            return cast(dict[str, object], value)
    if last_error is not None:
        raise last_error
    raise ValueError("Gate sidecar response contains no JSON object")


_GATE_RESULT_RETRY_QUESTION = (
    "你上一轮的门禁结果不符合固定返回契约，不能使用。请现在重新返回一份简洁结果："
    "只能输出一个 RFC 8259 JSON 对象，不能包含其他文字。对象必须包含 decision、summary、"
    "reasons、evidence 和 details；decision 只能是 PASS、FAIL 或 ERROR。summary、"
    "reasons 和 evidence 中所有面向人的文字必须使用中文。证据缺失、不完整、冲突或不足以"
    "证明通过时必须使用 FAIL，不能使用 INSUFFICIENT_EVIDENCE 等其他值；只有技术上无法"
    "完成审查时才能使用 ERROR。不得引用或复述候选产物正文。"
)


def _script(
    language: SandboxLanguage, code: str, context: dict[str, Any], timeout: int
) -> GateResult:
    execution = get_sandbox().execute(language, code, context, timeout)
    if execution.status == "TIMEOUT":
        return _error(execution.error or "Gate timed out", log=execution.log, code="GATE_TIMEOUT")
    if execution.status == "ERROR":
        return _error(execution.error or "Gate execution failed", log=execution.log)
    return _normalize(execution.result)


def _python(code: str, context: dict[str, Any], timeout: int) -> GateResult:
    return _script("PYTHON", code, context, timeout)


@dataclass(frozen=True, slots=True)
class GateExecutionPlan:
    gate_type: str
    config: dict[str, Any]
    timeout: int
    prompt_provider: PromptProviderSnapshot | None = None
    preparation_error: GateResult | None = None
    # Flow executions populate these values with a newly-created, isolated
    # Agent Conversation.  Keeping it on the frozen plan means the worker can
    # perform external I/O without reading the primary Agent's history.
    sidecar_request: StartAttemptRequest | None = None
    sidecar_question: str | None = None
    sidecar_binding_id: str | None = None
    # Gate-review Conversations are durable audit evidence. Other sidecars
    # (for example automatic transition selection) remain disposable.
    retain_sidecar: bool = False


def prepare_gate(
    db: Session, gate_type: str, config: dict[str, Any], timeout_seconds: int
) -> GateExecutionPlan:
    """Freeze all database-backed gate inputs before external execution."""

    timeout = max(1, min(int(timeout_seconds), 300))
    normalized = dict(config)
    if gate_type != "PROMPT":
        return GateExecutionPlan(gate_type, normalized, timeout)
    provider_id = str(normalized.get("model_provider_id") or "")
    prompt = str(normalized.get("prompt") or "")
    if not provider_id or not prompt:
        return GateExecutionPlan(
            gate_type,
            normalized,
            timeout,
            preparation_error=_error(
                "Prompt gate requires model_provider_id and prompt",
                code="GATE_CONFIG_INVALID",
            ),
        )
    try:
        provider = prompt_provider_snapshot(
            db, provider_id, str(normalized.get("model_name") or "") or None
        )
    except (ValueError, Exception) as exc:
        # Domain lookup errors and invalid provider configuration are normalized
        # into a gate result rather than escaping the worker task.
        return GateExecutionPlan(
            gate_type,
            normalized,
            timeout,
            preparation_error=_error(
                "Prompt gate model provider was not found or has no enabled model",
                log=str(exc),
                code="GATE_CONFIG_INVALID",
            ),
        )
    return GateExecutionPlan(gate_type, normalized, timeout, provider)


def _prompt(plan: GateExecutionPlan, context: dict[str, Any]) -> GateResult:
    provider = plan.prompt_provider
    prompt = str(plan.config.get("prompt") or "")
    if provider is None:
        return plan.preparation_error or _error(
            "Prompt gate provider is unavailable", code="GATE_CONFIG_INVALID"
        )
    system = (
        "评估工作流门禁。只能返回一个 JSON 对象，其中包含 decision（PASS、FAIL 或 ERROR）、"
        "summary、reasons、evidence 和 details；所有面向人的文字必须使用中文。"
    )
    user = prompt + "\n\nContext:\n" + json.dumps(context, ensure_ascii=False)
    payload = {
        "model": provider.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": user,
            },
        ],
    }
    try:
        if provider.protocol == "RESPONSES":
            return _prompt_responses(provider, system, user, plan.timeout)
        with httpx.Client(timeout=plan.timeout, follow_redirects=False) as client:
            response = client.post(
                f"{provider.base_url}/chat/completions",
                headers=provider.headers,
                json=payload,
            )
            response.raise_for_status()
        body = cast(dict[str, Any], response.json())
        content = body["choices"][0]["message"]["content"]
        return _normalize(json.loads(str(content)))
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _error(
            "Prompt gate execution failed", log=str(exc), code="GATE_EXECUTOR_UNAVAILABLE"
        )


def _prompt_responses(
    provider: PromptProviderSnapshot, system: str, user: str, timeout: int
) -> GateResult:
    payload = {
        "model": provider.model,
        "stream": True,
        "store": False,
        "reasoning": {"effort": "low"},
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [{"type": "input_text", "text": user}]},
        ],
    }
    deltas: list[str] = []
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream(
                "POST",
                f"{provider.base_url}/responses",
                headers={**provider.headers, "Accept": "text/event-stream"},
                json=payload,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = cast(dict[str, Any], json.loads(raw))
                    if event.get("type") == "response.output_text.delta" and isinstance(
                        event.get("delta"), str
                    ):
                        deltas.append(cast(str, event["delta"]))
                    elif event.get("type") in {"error", "response.failed"}:
                        raise ValueError(str(event.get("error") or event))
        return _normalize(json.loads("".join(deltas)))
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error(
            "Prompt gate execution failed", log=str(exc), code="GATE_EXECUTOR_UNAVAILABLE"
        )


def execute_gate_plan(plan: GateExecutionPlan, context: dict[str, Any]) -> GateResult:
    """Execute a frozen plan without reading from the database."""

    if plan.preparation_error is not None:
        return plan.preparation_error
    if plan.sidecar_request is not None and plan.sidecar_question is not None:
        return _sidecar_agent(plan)
    if plan.gate_type == "PLATFORM_OUTPUT_CONTRACT":
        return _platform_output_contract(context)
    if plan.gate_type == "PYTHON":
        return _python(str(plan.config.get("code") or ""), context, plan.timeout)
    if plan.gate_type == "PROMPT":
        return _prompt(plan, context)
    return _error(f"Unsupported gate type: {plan.gate_type}", code="GATE_CONFIG_INVALID")


def _platform_output_contract(context: dict[str, Any]) -> GateResult:
    """Validate delivery shape and frozen port mappings without judging content.

    This platform-owned check deliberately does not invoke a model, inspect an
    Artifact preview, or impose document-quality requirements.  Those are
    author-owned END gate concerns.
    """

    node = cast(dict[str, Any], context.get("node") or {})
    declared_outputs = {
        str(field.get("field_key") or ""): cast(dict[str, Any], field)
        for field in cast(list[object], node.get("outputs") or [])
        if isinstance(field, dict) and str(field.get("field_key") or "")
    }
    actual_outputs = {
        str(item.get("field_key") or ""): cast(dict[str, Any], item)
        for item in cast(list[object], context.get("outputs") or [])
        if isinstance(item, dict) and str(item.get("field_key") or "")
    }
    reasons: list[str] = []
    evidence: list[dict[str, Any]] = []
    selected_ids: list[str] = []

    for field_key, declared in declared_outputs.items():
        artifact = actual_outputs.get(field_key)
        expected_type = str(declared.get("data_type") or "")
        if artifact is None:
            reasons.append(f"缺少声明输出：{field_key}")
            evidence.append(
                {
                    "kind": "OUTPUT",
                    "status": "FAIL",
                    "field_key": field_key,
                    "expected_type": expected_type,
                    "reason": "未生成该声明输出",
                }
            )
            continue
        actual_type = str(artifact.get("artifact_type") or "")
        if actual_type != expected_type:
            reasons.append(f"输出 {field_key} 类型不匹配：期望 {expected_type}，实际 {actual_type}")
            evidence.append(
                {
                    "kind": "OUTPUT",
                    "status": "FAIL",
                    "field_key": field_key,
                    "expected_type": expected_type,
                    "actual_type": actual_type,
                    "reason": "产物类型与节点声明不一致",
                }
            )
            continue
        artifact_id = str(artifact.get("id") or "")
        if artifact_id:
            selected_ids.append(artifact_id)
        evidence.append(
            {
                "kind": "OUTPUT",
                "status": "PASS",
                "field_key": field_key,
                "artifact_id": artifact_id,
                "expected_type": expected_type,
                "actual_type": actual_type,
                "reason": "已生成正式产物，类型与节点声明一致",
            }
        )

    for consumer in cast(list[object], context.get("downstream_consumers") or []):
        if not isinstance(consumer, dict):
            continue
        consumer_key = str(consumer.get("instance_key") or "下游节点")
        for mapping in cast(list[object], consumer.get("mappings") or []):
            if not isinstance(mapping, dict):
                continue
            source_key = str(mapping.get("source_output_key") or "")
            target_input = cast(dict[str, Any], mapping.get("target_input") or {})
            target_key = str(target_input.get("field_key") or "")
            artifact = actual_outputs.get(source_key)
            if artifact is None:
                reasons.append(f"映射到 {consumer_key}.{target_key} 的输出 {source_key} 不存在")
                evidence.append(
                    {
                        "kind": "MAPPING",
                        "status": "FAIL",
                        "source_output_key": source_key,
                        "target": f"{consumer_key}.{target_key}",
                        "reason": "来源输出不存在，无法绑定下游输入",
                    }
                )
                continue
            if target_input.get("declared") is False:
                reasons.append(f"下游输入不存在：{consumer_key}.{target_key}")
                evidence.append(
                    {
                        "kind": "MAPPING",
                        "status": "FAIL",
                        "source_output_key": source_key,
                        "target": f"{consumer_key}.{target_key}",
                        "reason": "冻结快照中不存在该下游输入",
                    }
                )
                continue
            actual_type = str(artifact.get("artifact_type") or "")
            expected_type = str(target_input.get("data_type") or "")
            if expected_type and actual_type != expected_type:
                reasons.append(
                    f"映射类型不匹配：{source_key}（{actual_type}）不能绑定到 "
                    f"{consumer_key}.{target_key}（{expected_type}）"
                )
                evidence.append(
                    {
                        "kind": "MAPPING",
                        "status": "FAIL",
                        "source_output_key": source_key,
                        "target": f"{consumer_key}.{target_key}",
                        "expected_type": expected_type,
                        "actual_type": actual_type,
                        "reason": "来源产物类型不能绑定到下游输入",
                    }
                )
                continue
            evidence.append(
                {
                    "kind": "MAPPING",
                    "status": "PASS",
                    "source_output_key": source_key,
                    "target": f"{consumer_key}.{target_key}",
                    "expected_type": expected_type,
                    "actual_type": actual_type,
                    "reason": "来源产物可按冻结端口映射绑定到下游输入",
                }
            )

    if reasons:
        return GateResult(
            "FAIL",
            "平台交付与端口映射校验未通过",
            reasons,
            evidence,
            {"selected_output_artifact_ids": selected_ids},
        )
    return GateResult(
        "PASS",
        "平台交付与端口映射校验通过。",
        [],
        evidence,
        {"selected_output_artifact_ids": selected_ids},
    )


def _sidecar_agent(plan: GateExecutionPlan) -> GateResult:
    """Run one gate through its own native Agent conversation.

    This is intentionally not a provider HTTP call and not a platform Python
    runner.  The prepared request has a distinct conversation id, capability
    materialization directory and frozen Agent configuration.
    """

    assert plan.sidecar_request is not None and plan.sidecar_question is not None
    runtime = get_runtime()
    handle = None
    sidecar_available = False

    def with_sidecar(result: GateResult) -> GateResult:
        usage: tuple[RuntimeUsageSnapshot, ...] = ()
        if sidecar_available and handle is not None:
            try:
                # A native event read is the formal OpenHands state projection
                # that carries accumulated_token_usage for the whole sidecar
                # tree (primary Agent, tasks and condenser).  Capture it now
                # so a completed gate has an auditable cost even when nobody
                # opens its transcript later.
                usage = runtime.read_active_events(handle).usage
            except Exception:
                # Usage projection must not replace the Gate's authoritative
                # decision or hide an otherwise valid completed review.
                pass
        return replace(result, sidecar_available=sidecar_available, sidecar_usage=usage)

    try:
        handle = runtime.create_conversation(plan.sidecar_request)
        if handle.conversation_id != plan.sidecar_request.conversation_id:
            return with_sidecar(
                _error(
                    "Gate sidecar Conversation identity drifted",
                    code="GATE_EXECUTOR_UNAVAILABLE",
                )
            )
        sidecar_available = True
        runtime.reload_conversation(handle)
        answer = _run_recorded_gate_turn(
            runtime, handle, plan.sidecar_question, timeout_seconds=float(plan.timeout)
        )
        try:
            result = _normalize(_decode_gate_response(answer))
        except (ValueError, json.JSONDecodeError):
            result = _error("Gate sidecar returned invalid JSON", code="GATE_RESULT_INVALID")
        if result.error_code != "GATE_RESULT_INVALID":
            return with_sidecar(result)
        # A malformed JSON envelope or an otherwise valid JSON object with an
        # unsupported decision must never be treated as a decision. Let this
        # same isolated Gate Agent correct it once.  This is deliberately a
        # second native user turn, rather than OpenHands' stateless
        # ``ask_agent`` endpoint, so the retained sidecar Conversation remains
        # an auditable record of both the original judgement and correction.
        corrected = _run_recorded_gate_turn(
            runtime, handle, _GATE_RESULT_RETRY_QUESTION, timeout_seconds=float(plan.timeout)
        )
        return with_sidecar(_normalize(_decode_gate_response(corrected)))
    except (ValueError, json.JSONDecodeError) as exc:
        return with_sidecar(
            _error(
                "Gate sidecar returned invalid JSON",
                log=str(exc),
                code="GATE_RESULT_INVALID",
            )
        )
    except Exception as exc:
        return with_sidecar(
            _error(
                "Gate sidecar execution failed",
                log=str(exc),
                code="GATE_EXECUTOR_UNAVAILABLE",
            )
        )
    finally:
        if handle is not None and (not plan.retain_sidecar or not sidecar_available):
            try:
                runtime.delete_conversation(handle)
            except Exception:
                pass


def _run_recorded_gate_turn(
    runtime: Any, handle: Any, question: str, *, timeout_seconds: float
) -> str:
    """Send one native turn and return its formal final reply.

    OpenHands' ``ask_agent`` API is expressly stateless: it neither persists
    the request nor creates events.  It therefore cannot back the Gate detail
    transcript.  A Gate is a retained, read-only Conversation, so its review
    prompt and the Agent's answer must instead travel through the ordinary
    native message/event lifecycle.
    """

    runtime.send_message(handle, question)
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while True:
        observed = runtime.inspect(handle)
        if observed.status == "COMPLETED":
            answer = observed.final_message
            if isinstance(answer, str) and answer.strip():
                return answer
            raise ValueError("Gate sidecar completed without a final response")
        if observed.status in {
            "FAILED",
            "CANCELLED",
            "CONFIRMATION_REQUIRED",
            "HUMAN_INPUT_REQUIRED",
        }:
            detail = observed.error or observed.human_question or observed.status
            raise ValueError(f"Gate sidecar native turn did not complete: {detail}")
        if time.monotonic() >= deadline:
            raise ValueError("Gate sidecar native turn timed out")
        # ``send_message`` accepts a formal user event, but the Runtime owns
        # completion and event persistence. Poll its current native state
        # rather than inferring completion from transport acceptance.
        time.sleep(0.1)


def execute_gate(
    db: Session,
    gate_type: str,
    config: dict[str, Any],
    context: dict[str, Any],
    timeout_seconds: int,
) -> GateResult:
    """Compatibility API for direct tests and inline execution."""

    return execute_gate_plan(prepare_gate(db, gate_type, config, timeout_seconds), context)
