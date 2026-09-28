from __future__ import annotations

import logging

import pytest

from flowweave.modules.agent_sessions.application import condensation
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.dependencies import runtime_context
from flowweave.shared.domain.errors import DomainError


@pytest.mark.parametrize(
    ("error", "reason", "outcome_unknown"),
    (
        (
            DomainError(
                "OPENHANDS_REQUEST_FAILED", "private upstream detail", 502, {"status_code": 429}
            ),
            "runtime_rate_limited",
            False,
        ),
        (
            DomainError(
                "OPENHANDS_REQUEST_FAILED", "private upstream detail", 502, {"status_code": 503}
            ),
            "runtime_service_unavailable",
            False,
        ),
        (
            DomainError(
                "OPENHANDS_REQUEST_FAILED", "private upstream detail", 502, {"status_code": 401}
            ),
            "runtime_auth_failed",
            False,
        ),
        (
            DomainError(
                "OPENHANDS_REQUEST_FAILED", "private upstream detail", 502, {"status_code": 400}
            ),
            "runtime_request_rejected",
            False,
        ),
        (
            DomainError(
                "EXECUTOR_UNAVAILABLE",
                "private upstream detail",
                503,
                {"transport_failure": "timeout"},
            ),
            "runtime_timeout_unknown",
            True,
        ),
        (
            DomainError(
                "EXECUTOR_UNAVAILABLE",
                "private upstream detail",
                503,
                {"transport_failure": "connection"},
            ),
            "runtime_connection_unknown",
            True,
        ),
        (
            DomainError("OPENHANDS_RESPONSE_INVALID", "private upstream detail", 502),
            "runtime_response_invalid",
            False,
        ),
    ),
)
def test_condensation_runtime_failures_have_safe_reason(error, reason, outcome_unknown):
    classified = condensation._condensation_runtime_failure(error)

    assert classified.code == f"CONDENSATION_{reason.upper()}"
    assert classified.details == {"outcome_unknown": outcome_unknown}
    assert (
        condensation.condensation_task_failure_reason(f"{classified.code}: {classified.message}")
        == reason
    )
    assert "private upstream detail" not in classified.message


def test_condensation_ignores_unrecognized_task_failure_text():
    assert (
        condensation.condensation_task_failure_reason("OPENHANDS_REQUEST_FAILED: private") is None
    )
    assert condensation.condensation_task_failure_reason("secret error") is None


def test_condensation_logs_safe_failure_without_runtime_detail(monkeypatch, caplog):
    handle = RuntimeHandle(
        job_id="agent-workspace:workspace-1",
        conversation_id="10000000-0000-4000-8000-000000000002",
        runtime_resource_name="runtime-1",
    )

    class FailingRuntime:
        def can_accept_input(self, actual_handle):
            assert actual_handle is handle
            return True

        def condense(self, actual_handle):
            assert actual_handle is handle
            raise DomainError(
                "OPENHANDS_REQUEST_FAILED",
                "https://private.example/v1 Bearer sk-secret",
                502,
                {"status_code": 503},
            )

    monkeypatch.setattr(condensation, "resolve_handle", lambda _db, _binding_id: handle)
    monkeypatch.setattr(condensation, "lease_is_current", lambda _db, _lease: True)
    caplog.set_level(logging.INFO, logger="flowweave.agent_conversation_diagnostics")

    class Session:
        def rollback(self):
            pass

    with runtime_context(FailingRuntime()), pytest.raises(DomainError) as raised:
        condensation.process_manual_condensation(Session(), "binding-1", {}, object())

    assert raised.value.code == "CONDENSATION_RUNTIME_SERVICE_UNAVAILABLE"
    message = next(
        record.getMessage()
        for record in caplog.records
        if "agent_conversation_diagnostic" in record.getMessage()
        and '"operation":"condensation_completed"' in record.getMessage()
    )
    assert '"error_kind":"CONDENSATION_RUNTIME_SERVICE_UNAVAILABLE"' in message
    assert "private.example" not in message
    assert "sk-secret" not in message
