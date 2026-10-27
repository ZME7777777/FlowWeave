"""Two short authorization phases around native launcher verification."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.child_parent import (
    ParentCapability,
    denied,
    read_parent_capability,
)
from flowweave.modules.agent_sessions.application.child_relations import require_ordinary_binding
from flowweave.modules.agent_sessions.application.flow_node_host import (
    assert_flow_node_session_writable,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentSidebarConversation,
)
from flowweave.modules.agent_workspaces.infrastructure.models import (
    AgentWorkspace,
    AgentWorkspaceRuntime,
)
from flowweave.modules.sandboxes import public as sandboxes
from flowweave.modules.users.application.security import Principal, tenant_user
from flowweave.modules.users.application.service import agent_session_principal
from flowweave.runtime.auth import derive_runtime_session_key
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.child_creation import NativeChildInvocation, NativeChildLaunchRequest
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError
from flowweave.shared.models import FlowRunRuntime, ManagedSandbox, NodeAttempt, RunSnapshot
from flowweave.shared.settings import get_settings


@dataclass(frozen=True, slots=True)
class AuthorizedParent:
    principal: Principal
    capability: ParentCapability
    handle: RuntimeHandle


def _resolve_launcher(
    db: Session,
    *,
    owner_id: UUID,
    binding_id: UUID,
    runtime_key: SecretStr,
) -> AuthorizedParent:
    owner, parent_id = str(owner_id), str(binding_id)
    with tenant_user(owner):
        principal = agent_session_principal(db, owner)
        binding = db.scalar(
            select(AgentConversationBinding).where(
                AgentConversationBinding.owner_user_id == owner,
                AgentConversationBinding.id == parent_id,
                AgentConversationBinding.lifecycle == "ACTIVE",
            )
        )
        if principal is None or binding is None:
            raise denied()
        frozen = read_parent_capability(binding)
        require_ordinary_binding(db, binding)
        if binding.create_idempotency_key.startswith("gate-sidecar:") or db.scalar(
            select(AgentSidebarConversation.id).where(
                AgentSidebarConversation.sidebar_binding_id == parent_id
            )
        ):
            raise denied()
        if binding.host_kind == "AGENT_WORKSPACE":
            from flowweave.modules.agent_sessions.application.conversations import (
                usage_reconciliation_handle,
            )

            workspace = db.get(AgentWorkspace, binding.host_id)
            session = db.get(AgentWorkspaceRuntime, binding.runtime_session_id)
            if (
                workspace is None
                or workspace.desired_state != "RUNNING"
                or session is None
                or session.workspace_id != binding.host_id
                or binding.workspace_id != binding.host_id
            ):
                raise denied()
            handle = usage_reconciliation_handle(db, binding)
            resource = db.get(ManagedSandbox, handle.runtime_resource_id)
            if resource is None or resource.image_reference != frozen.runtime_image_digest:
                raise denied()
        elif binding.host_kind == "FLOW_NODE":
            assert_flow_node_session_writable(
                db, flow_run_id=binding.host_id, attempt_id=binding.conversation_scope_id
            )
            attempt = db.get(NodeAttempt, binding.conversation_scope_id)
            snapshot = db.get(RunSnapshot, attempt.snapshot_id) if attempt is not None else None
            if (
                snapshot is None
                or snapshot.flow_run_id != binding.host_id
                or snapshot.runtime_manifest_json != frozen.runtime_manifest
                or snapshot.runtime_manifest_hash != frozen.policy.manifest_digest
            ):
                raise denied()
            connection = sandboxes.active_node_runtime_connection(
                db,
                flow_run_id=binding.host_id,
                node_attempt_id=binding.conversation_scope_id,
            )
            session = db.get(FlowRunRuntime, binding.runtime_session_id)
            if (
                connection.runtime_session_id != binding.runtime_session_id
                or connection.protocol_profile.value != "OH_FW_V3"
                or connection.generation != frozen.policy.generation
            ):
                raise denied()
            handle = RuntimeHandle(
                f"env-chat:{connection.resource_name}",
                binding.openhands_conversation_id,
                runtime_resource_id=connection.managed_runtime_id,
                runtime_resource_name=connection.resource_name,
                workspace_root=frozen.working_directory,
                protocol_profile="OH_FW_V3",
            )
        else:
            raise denied()
        if (
            session is None
            or session.status != "ACTIVE"
            or session.active_generation != frozen.policy.generation
            or session.runtime_image_digest != frozen.runtime_image_digest
            or handle.protocol_profile != "OH_FW_V3"
        ):
            raise denied()
        expected = derive_runtime_session_key(
            get_settings().openhands_session_api_key,
            get_settings().sandbox_manager_scope,
            handle.runtime_resource_name,
        )
        if not hmac.compare_digest(runtime_key.get_secret_value(), expected):
            raise denied()
        return AuthorizedParent(
            principal, frozen, replace(handle, workspace_root=frozen.working_directory)
        )


def prepare_launcher(
    db: Session,
    *,
    owner_id: UUID,
    binding_id: UUID,
    runtime_key: SecretStr,
) -> AuthorizedParent:
    try:
        return _resolve_launcher(
            db, owner_id=owner_id, binding_id=binding_id, runtime_key=runtime_key
        )
    except (DomainError, ValueError):
        raise denied() from None


def verify_launcher(
    prepared: AuthorizedParent, request: NativeChildLaunchRequest
) -> NativeChildInvocation:
    """No database Session crosses this native I/O boundary."""
    return get_runtime().verify_child_launcher(prepared.handle, request)


def project_launcher(
    db: Session,
    prepared: AuthorizedParent,
    invocation: NativeChildInvocation,
    *,
    runtime_key: SecretStr,
) -> AuthorizedParent:
    fresh = prepare_launcher(
        db,
        owner_id=prepared.capability.owner_user_id,
        binding_id=prepared.capability.binding_id,
        runtime_key=runtime_key,
    )
    if (
        fresh != prepared
        or invocation.policy != fresh.capability.policy
        or invocation.audience != fresh.capability.launch_url
    ):
        raise denied()
    return fresh
