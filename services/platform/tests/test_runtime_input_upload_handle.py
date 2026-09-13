from uuid import uuid4

import pytest

from flowweave.modules.agent_sessions.application.conversations import validate_attachment_owners
from flowweave.modules.agent_sessions.application.flow_node_conversations import (
    validate_attempt_input_attachments,
)
from flowweave.modules.orchestration.application.service import _runtime_input_upload_handle
from flowweave.runtime.base import StartAttemptRequest
from flowweave.shared.errors import DomainError


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


def test_runtime_file_inputs_are_owned_by_the_attempt_not_a_gate_sidecar_binding():
    attempt_id = "11111111-1111-4111-8111-111111111111"
    execution_binding_id = "22222222-2222-4222-8222-222222222222"
    gate_sidecar_binding_id = "33333333-3333-4333-8333-333333333333"
    workspace_root = "/runtime/workspace/project/44444444-4444-4444-8444-444444444444"
    attachments = (
        {
            "path": f"{workspace_root}/uploads/{attempt_id}-" + "a" * 32 + "--input.pdf",
            "filename": "input.pdf",
            "mime_type": "application/pdf",
            "byte_size": 12,
        },
    )

    assert (
        validate_attempt_input_attachments(attempt_id, attachments, workspace_root=workspace_root)
        == attachments
    )
    with pytest.raises(DomainError) as raised:
        validate_attachment_owners(
            gate_sidecar_binding_id, attachments, workspace_root=workspace_root
        )
    assert raised.value.code == "AGENT_ATTACHMENT_INVALID"

    for private_binding_id in (execution_binding_id, gate_sidecar_binding_id):
        private_attachment = (
            {
                **attachments[0],
                "path": f"{workspace_root}/uploads/{private_binding_id}-"
                + "b" * 32
                + "--input.pdf",
            },
        )
        with pytest.raises(DomainError) as raised:
            validate_attempt_input_attachments(
                attempt_id, private_attachment, workspace_root=workspace_root
            )
        assert raised.value.code == "RUNTIME_ARTIFACT_ATTACHMENT_INVALID"
