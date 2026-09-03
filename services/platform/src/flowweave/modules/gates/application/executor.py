from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import httpx
from sqlalchemy.orm import Session

from flowweave.modules.model_providers.public import (
    PromptProviderSnapshot,
    prompt_provider_snapshot,
)
from flowweave.runtime.base import StartAttemptRequest
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
    decision = str(mapping.get("decision", "ERROR")).upper()
    if decision not in DECISIONS:
        return _error("Gate decision must be PASS, FAIL, or ERROR", code="GATE_RESULT_INVALID")
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
            return value
    if last_error is not None:
        raise last_error
    raise ValueError("Gate sidecar response contains no JSON object")


_GATE_JSON_RETRY_QUESTION = (
    "Your previous gate result was not valid JSON and cannot be used. Return a "
    "fresh, concise result now: exactly one RFC 8259 JSON object and nothing "
    "else. Include decision, summary, reasons, evidence, and details. Do not "
    "quote or reproduce any candidate artifact content."
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
        "Evaluate a workflow gate. Return only JSON with decision PASS, FAIL, or "
        "ERROR plus summary, reasons, evidence, and details."
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
    if plan.gate_type == "PYTHON":
        return _python(str(plan.config.get("code") or ""), context, plan.timeout)
    if plan.gate_type == "PROMPT":
        return _prompt(plan, context)
    return _error(f"Unsupported gate type: {plan.gate_type}", code="GATE_CONFIG_INVALID")


def _sidecar_agent(plan: GateExecutionPlan) -> GateResult:
    """Run one gate through its own native Agent conversation.

    This is intentionally not a provider HTTP call and not a platform Python
    runner.  The prepared request has a distinct conversation id, capability
    materialization directory and frozen Agent configuration.
    """

    assert plan.sidecar_request is not None and plan.sidecar_question is not None
    runtime = get_runtime()
    handle = None
    try:
        handle = runtime.create_conversation(plan.sidecar_request)
        if handle.conversation_id != plan.sidecar_request.conversation_id:
            return _error(
                "Gate sidecar Conversation identity drifted",
                code="GATE_EXECUTOR_UNAVAILABLE",
            )
        runtime.reload_conversation(handle)
        answer = runtime.ask_agent(
            handle, plan.sidecar_question, timeout_seconds=float(plan.timeout)
        ).response
        try:
            return _normalize(_decode_gate_response(answer))
        except (ValueError, json.JSONDecodeError):
            # ``ask_agent`` returns rendered model text.  A malformed result
            # must never be repaired or treated as a decision, but the same
            # isolated Gate Agent may make one short native correction turn.
            # It retains the frozen context while the retry prompt prevents
            # copying large candidate text back into its JSON envelope.
            corrected = runtime.ask_agent(
                handle, _GATE_JSON_RETRY_QUESTION, timeout_seconds=float(plan.timeout)
            ).response
            return _normalize(_decode_gate_response(corrected))
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(
            "Gate sidecar returned invalid JSON", log=str(exc), code="GATE_RESULT_INVALID"
        )
    except Exception as exc:
        return _error(
            "Gate sidecar execution failed",
            log=str(exc),
            code="GATE_EXECUTOR_UNAVAILABLE",
        )
    finally:
        if handle is not None:
            try:
                runtime.delete_conversation(handle)
            except Exception:
                pass


def execute_gate(
    db: Session,
    gate_type: str,
    config: dict[str, Any],
    context: dict[str, Any],
    timeout_seconds: int,
) -> GateResult:
    """Compatibility API for direct tests and inline execution."""

    return execute_gate_plan(prepare_gate(db, gate_type, config, timeout_seconds), context)
