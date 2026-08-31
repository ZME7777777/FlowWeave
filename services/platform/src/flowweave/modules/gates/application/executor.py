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


def _javascript(code: str, context: dict[str, Any], timeout: int) -> GateResult:
    return _script("JAVASCRIPT", code, context, timeout)


@dataclass(frozen=True, slots=True)
class GateExecutionPlan:
    gate_type: str
    config: dict[str, Any]
    timeout: int
    prompt_provider: PromptProviderSnapshot | None = None
    preparation_error: GateResult | None = None


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
    if plan.gate_type == "PYTHON":
        return _python(str(plan.config.get("code") or ""), context, plan.timeout)
    if plan.gate_type == "JAVASCRIPT":
        return _javascript(str(plan.config.get("code") or ""), context, plan.timeout)
    if plan.gate_type == "PROMPT":
        return _prompt(plan, context)
    return _error(f"Unsupported gate type: {plan.gate_type}", code="GATE_CONFIG_INVALID")


def execute_gate(
    db: Session,
    gate_type: str,
    config: dict[str, Any],
    context: dict[str, Any],
    timeout_seconds: int,
) -> GateResult:
    """Compatibility API for direct tests and inline execution."""

    return execute_gate_plan(prepare_gate(db, gate_type, config, timeout_seconds), context)
