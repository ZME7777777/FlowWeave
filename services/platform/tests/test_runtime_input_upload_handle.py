from uuid import uuid4

import pytest

from flowweave.modules.orchestration.application.service import _runtime_input_upload_handle
from flowweave.runtime.base import StartAttemptRequest


@pytest.fixture(autouse=True)
def database():
    """The pre-start handle is a pure request-to-handle projection."""

    yield


def test_runtime_input_upload_handle_preserves_record_workspace_root():
    conversation_id = str(uuid4())
    request = StartAttemptRequest(
        attempt_id="attempt-file-input",
        execution_key="attempt:attempt-file-input:start",
        node={},
        bindings=[],
        workspace_ref="/data/workspaces/record",
        conversation_id=conversation_id,
        runtime_sandbox_id="11111111-1111-4111-8111-111111111111",
        runtime_resource_name="flowweave-run-generation-7",
        workspace_root="/runtime/workspace/22222222-2222-4222-8222-222222222222",
    )

    handle = _runtime_input_upload_handle(request)

    assert handle.conversation_id == conversation_id
    assert handle.workspace_root == request.workspace_root
