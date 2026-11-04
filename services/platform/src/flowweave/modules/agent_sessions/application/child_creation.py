"""Durable child locators around native prepare/release, never an agent executor."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import child_launcher
from flowweave.modules.agent_sessions.application.child_parent import denied
from flowweave.modules.agent_sessions.application.history_integration import (
    SECRET_NAME,
    SERVER_NAME,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
    AgentConversationCapability,
)
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.child_contract import CHILD_TOOL_NAMES
from flowweave.runtime.child_creation import (
    NativeChildConfiguration,
    NativeChildInvocation,
    NativeChildLaunchRequest,
    NativeChildPreparation,
    child_contract_error,
)
from flowweave.runtime.child_governance import (
    NativeChildPreparationOverrides,
    NativeChildResolution,
    NativeGovernedChildPreparation,
    NativeParentConfiguration,
)
from flowweave.runtime.dependencies import get_runtime
from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

POLICY_VERSION = "binding-isolated-v1"


@dataclass(frozen=True, slots=True)
class VerifiedCreation:
    resolved: NativeChildResolution
    configuration: NativeParentConfiguration


@dataclass(frozen=True, slots=True)
class ReservedChild:
    parent: child_launcher.AuthorizedParent
    resolved: NativeChildResolution
    binding_id: UUID
    expected_agent_digest: str
    expected_persistence_dir: str
    governed: bool

    @property
    def handle(self) -> RuntimeHandle:
        return replace(
            self.parent.handle, conversation_id=str(self.resolved.identity.conversation_id)
        )

    def overrides(self) -> NativeChildPreparationOverrides:
        return NativeChildPreparationOverrides(
            governance_id=self.binding_id,
            remove_secrets=frozenset({SECRET_NAME}),
            remove_mcp_servers=frozenset({SERVER_NAME}),
        )


@dataclass(frozen=True, slots=True)
class PreparedChild:
    preparation: NativeGovernedChildPreparation
    configuration: NativeChildConfiguration


def unavailable() -> DomainError:
    return DomainError(
        "AGENT_CHILD_CREATION_NOT_ENABLED",
        "当前冻结协议尚未启用子会话创建治理",
        503,
        {"outcome_unknown": False},
    )


def conflict() -> DomainError:
    return DomainError("AGENT_CHILD_CREATION_CONFLICT", "子会话创建身份或冻结配置不匹配", 409)


def read_creation(
    parent: child_launcher.AuthorizedParent, request: NativeChildLaunchRequest
) -> VerifiedCreation | NativeChildInvocation:
    verified = child_launcher.verify_launcher(parent, request)
    if parent.capability.profile != "OH_FW_V4":
        # Preserve the older authorization boundary and its explicit product gate.
        if not isinstance(verified, NativeChildInvocation):
            raise child_contract_error()
        return verified
    if not isinstance(verified, NativeChildResolution):
        raise child_contract_error()
    config = get_runtime().parent_conversation_configuration(parent.handle)
    if (
        config.id != verified.identity.parent_conversation_id
        or config.workspace.working_dir != parent.capability.working_directory
        or not config.persistence_dir
    ):
        raise conflict()
    return VerifiedCreation(verified, config)


def expected_agent(parent: NativeParentConfiguration) -> dict[str, Any]:
    if parent.agent.get("kind") != "Agent":
        # ACP activation is conversation-bound and cannot be inherited as a secret.
        raise DomainError(
            "AGENT_CHILD_AGENT_UNSUPPORTED", "当前子创建契约需要原生 OpenHands Agent", 409
        )
    value = deepcopy(parent.agent)
    value["tools"] = [
        tool
        for tool in value.get("tools", [])
        if tool.get("name") not in {*CHILD_TOOL_NAMES, "start_child_conversation"}
    ]
    mcp = cast(dict[str, Any], value.get("mcp_config") or {})
    value["mcp_config"] = {name: config for name, config in mcp.items() if name != SERVER_NAME}
    context = value.get("agent_context")
    if isinstance(context, dict):
        context = cast(dict[str, Any], context)
        sources = cast(dict[str, Any], context.get("secrets") or {})
        context["secrets"] = {
            name: source for name, source in sources.items() if name != SECRET_NAME
        }
    return value


def agent_configuration_digest(agent: dict[str, Any]) -> str:
    # AgentContext regenerates this formal presentation value when finalizing a
    # child. It is not capability/config authority. Keep every other field.
    document = deepcopy(agent)
    context = document.get("agent_context")
    if isinstance(context, dict):
        cast(dict[str, Any], context).pop("current_datetime", None)
    return runtime_manifest_hash(document)


def reserve_creation(
    db: Session,
    parent: child_launcher.AuthorizedParent,
    verified: VerifiedCreation | NativeChildInvocation,
    *,
    runtime_key: SecretStr,
) -> ReservedChild | child_launcher.AuthorizedParent:
    resolved = verified.resolved if isinstance(verified, VerifiedCreation) else verified
    with tenant_user(str(parent.capability.owner_user_id)):
        fresh = child_launcher.project_launcher(db, parent, resolved, runtime_key=runtime_key)
        if not isinstance(verified, VerifiedCreation):
            return fresh
        parent_row = db.scalar(
            select(AgentConversationBinding)
            .where(
                AgentConversationBinding.id == str(fresh.capability.binding_id),
                AgentConversationBinding.owner_user_id == str(fresh.capability.owner_user_id),
                AgentConversationBinding.lifecycle.in_(("ACTIVE", "PROVISIONING")),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent_row is None:
            raise denied()
        identity = verified.resolved.identity
        claim = verified.resolved.invocation
        relation = db.scalar(
            select(AgentChildConversation).where(
                AgentChildConversation.parent_binding_id == parent_row.id,
                AgentChildConversation.creation_id == str(identity.creation_id),
            )
        )
        if relation is None:
            agent_digest = agent_configuration_digest(expected_agent(verified.configuration))
            persistence = str(
                PurePosixPath(verified.configuration.persistence_dir or "").parent
                / identity.conversation_id.hex
            )
            child_id = str(uuid4())
            child = AgentConversationBinding(
                id=child_id,
                owner_user_id=parent_row.owner_user_id,
                workspace_id=parent_row.workspace_id,
                host_kind=parent_row.host_kind,
                host_id=parent_row.host_id,
                conversation_scope_id=parent_row.conversation_scope_id,
                runtime_session_id=parent_row.runtime_session_id,
                flow_run_id=parent_row.flow_run_id,
                work_directory_version_id=parent_row.work_directory_version_id,
                working_directory=parent_row.working_directory,
                model_provider_id=parent_row.model_provider_id,
                model_name=parent_row.model_name,
                reasoning_effort=parent_row.reasoning_effort,
                fallback_models_json=deepcopy(parent_row.fallback_models_json or []),
                openhands_conversation_id=str(identity.conversation_id),
                create_idempotency_key=f"native-child:{parent_row.id}:{identity.creation_id}",
                display_title=None,
                lifecycle="PROVISIONING",
            )
            db.add(child)
            db.flush()
            for capability in db.scalars(
                select(AgentConversationCapability).where(
                    AgentConversationCapability.binding_id == parent_row.id
                )
            ):
                if capability.capability_key == SERVER_NAME:
                    continue
                db.add(
                    AgentConversationCapability(
                        binding_id=child.id,
                        capability_version_id=capability.capability_version_id,
                        capability_type=capability.capability_type,
                        capability_key=capability.capability_key,
                        digest=capability.digest,
                        position=capability.position,
                    )
                )
            relation = AgentChildConversation(
                owner_user_id=parent_row.owner_user_id,
                host_kind=parent_row.host_kind,
                host_id=parent_row.host_id,
                conversation_scope_id=parent_row.conversation_scope_id,
                runtime_session_id=parent_row.runtime_session_id,
                parent_binding_id=parent_row.id,
                child_binding_id=child_id,
                parent_conversation_id=str(identity.parent_conversation_id),
                child_conversation_id=str(identity.conversation_id),
                creation_id=str(identity.creation_id),
                action_id=claim.action_id,
                tool_call_id=claim.tool_call_id,
                accepted_generation=claim.policy.generation,
                manifest_digest=claim.policy.manifest_digest,
                creation_policy_version=POLICY_VERSION,
                request_digest=claim.request_digest,
                expected_agent_digest=agent_digest,
                expected_persistence_dir=persistence,
            )
            db.add(relation)
            db.flush()
        return _reservation(db, fresh, verified.resolved, relation)


def _reservation(
    db: Session,
    parent: child_launcher.AuthorizedParent,
    resolved: NativeChildResolution,
    relation: AgentChildConversation,
) -> ReservedChild:
    identity, claim = resolved.identity, resolved.invocation
    child = db.get(AgentConversationBinding, relation.child_binding_id)
    if (
        child is None
        or child.owner_user_id != str(parent.capability.owner_user_id)
        or relation.owner_user_id != str(parent.capability.owner_user_id)
        or relation.host_kind != parent.capability.host_kind
        or relation.host_id != str(parent.capability.host_id)
        or relation.conversation_scope_id != str(parent.capability.conversation_scope_id)
        or relation.runtime_session_id != str(parent.capability.policy.runtime_session_id)
        or relation.creation_policy_version != POLICY_VERSION
        or relation.parent_binding_id != str(parent.capability.binding_id)
        or relation.parent_conversation_id != str(identity.parent_conversation_id)
        or relation.child_conversation_id != str(identity.conversation_id)
        or relation.creation_id != str(identity.creation_id)
        or relation.action_id != claim.action_id
        or relation.tool_call_id != claim.tool_call_id
        or relation.accepted_generation != claim.policy.generation
        or relation.manifest_digest != claim.policy.manifest_digest
        or relation.request_digest != claim.request_digest
        or not relation.expected_agent_digest
        or not relation.expected_persistence_dir
        or child.openhands_conversation_id != relation.child_conversation_id
        or child.runtime_session_id != relation.runtime_session_id
        or child.working_directory != parent.capability.working_directory
        or child.lifecycle not in {"PROVISIONING", "ACTIVE"}
    ):
        raise conflict()
    return ReservedChild(
        parent,
        resolved,
        UUID(child.id),
        relation.expected_agent_digest,
        relation.expected_persistence_dir,
        child.lifecycle == "ACTIVE",
    )


def prepare_native(child: ReservedChild, request: NativeChildLaunchRequest) -> PreparedChild:
    runtime = get_runtime()
    prepared = runtime.prepare_governed_child_conversation(
        child.parent.handle, child.resolved.identity.creation_id, request, child.overrides()
    )
    configuration = runtime.child_conversation_configuration(child.handle)
    _verify_native(child, prepared)
    return PreparedChild(prepared, configuration)


def _verify_native(
    child: ReservedChild,
    prepared: NativeChildPreparation,
    configuration: NativeChildConfiguration | None = None,
) -> None:
    if (
        not isinstance(prepared, NativeGovernedChildPreparation)
        or prepared.receipt.identity != child.resolved.identity
        or prepared.receipt.request_digest != child.resolved.invocation.request_digest
        or prepared.governance_id != child.binding_id
        or prepared.workspace != child.parent.capability.working_directory
    ):
        raise conflict()
    if configuration is not None and (
        configuration.id != child.resolved.identity.conversation_id
        or configuration.parent_conversation_id != child.resolved.identity.parent_conversation_id
        or configuration.workspace.working_dir != child.parent.capability.working_directory
        or configuration.persistence_dir != child.expected_persistence_dir
        or (
            not child.governed
            and agent_configuration_digest(configuration.agent) != child.expected_agent_digest
        )
    ):
        raise conflict()


def reconcile_reservation(
    db: Session,
    child: ReservedChild,
    *,
    runtime_key: SecretStr,
) -> ReservedChild:
    with tenant_user(str(child.parent.capability.owner_user_id)):
        fresh = child_launcher.project_launcher(
            db, child.parent, child.resolved, runtime_key=runtime_key
        )
        relation = db.scalar(
            select(AgentChildConversation).where(
                AgentChildConversation.parent_binding_id == str(fresh.capability.binding_id),
                AgentChildConversation.child_binding_id == str(child.binding_id),
            )
        )
        if relation is None:
            raise conflict()
        return _reservation(db, fresh, child.resolved, relation)


def govern_creation(
    db: Session,
    child: ReservedChild,
    prepared: PreparedChild,
    *,
    runtime_key: SecretStr,
) -> ReservedChild:
    fresh = reconcile_reservation(db, child, runtime_key=runtime_key)
    _verify_native(fresh, prepared.preparation, prepared.configuration)
    with tenant_user(str(child.parent.capability.owner_user_id)):
        binding = db.get(AgentConversationBinding, str(child.binding_id))
        if binding is None:
            raise conflict()
        if not fresh.governed:
            if prepared.preparation.phase != "prepared":
                raise conflict()
            # ACTIVE here is governed locator readiness, never native run state.
            binding.lifecycle = "ACTIVE"
        db.flush()
    return replace(fresh, governed=True)


def release_native(
    child: ReservedChild, request: NativeChildLaunchRequest
) -> NativeChildPreparation:
    if not child.governed:
        raise conflict()
    prepared = get_runtime().release_child_conversation(
        child.parent.handle, child.resolved.identity.creation_id, request
    )
    _verify_native(child, prepared)
    if prepared.phase != "started":
        raise conflict()
    return prepared


def finish_creation(
    db: Session,
    child: ReservedChild,
    prepared: NativeChildPreparation,
    *,
    runtime_key: SecretStr,
) -> dict[str, object]:
    fresh = reconcile_reservation(db, child, runtime_key=runtime_key)
    _verify_native(fresh, prepared)
    if prepared.phase != "started":
        raise conflict()
    with tenant_user(str(child.parent.capability.owner_user_id)):
        binding = db.get(AgentConversationBinding, str(child.binding_id))
        if binding is None or not fresh.governed:
            raise conflict()
        if binding.last_message_at is None:
            binding.last_message_at = now()
            binding.terminal_reconciliation_pending = True
        db.flush()
        return {
            "conversation_id": str(child.resolved.identity.conversation_id),
            "parent_conversation_id": str(child.resolved.identity.parent_conversation_id),
            "status": "started",
            "workspace": child.parent.capability.working_directory,
            "isolation": "shared",
            "binding_id": str(child.binding_id),
        }
