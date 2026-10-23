from types import SimpleNamespace

import pytest

from flowweave.modules.agent_sessions.application.activity_reconciliation import (
    read_activity_completion,
)
from flowweave.runtime.base import RuntimeHandle
from flowweave.shared.errors import DomainError


@pytest.fixture(autouse=True)
def database():
    yield


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("AUTHORIZATION_DENIED", 403),
        ("RUNTIME_CONVERSATION_MISSING", 409),
        ("OPENHANDS_RESPONSE_INVALID", 502),
        ("RUNTIME_PROTOCOL_ERROR", 503),
    ],
)
def test_completion_reconciliation_does_not_hide_nontransient_errors(code, status):
    failure = DomainError(code, "controlled failure", status)

    def fail(_handle):
        raise failure

    with pytest.raises(DomainError) as caught:
        read_activity_completion(
            SimpleNamespace(read_active_events=fail),
            RuntimeHandle(job_id="test", conversation_id="test"),
        )
    assert caught.value is failure
