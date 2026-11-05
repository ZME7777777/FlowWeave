"""Read-only native identity evidence for a frozen parent's Runtime handoff."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.child_creation import POLICY_VERSION
from flowweave.modules.agent_sessions.application.child_parent import (
    GovernedParentCapability,
    read_parent_capability,
)
from flowweave.modules.agent_sessions.application.child_relations import require_direct_child
from flowweave.modules.agent_sessions.application.host import AgentSessionHostContext
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentChildConversation,
    AgentConversationBinding,
)
from flowweave.modules.users.application.security import (
    tenant_bypass,
    tenant_user,
    user_runtime_project_root,
)
from flowweave.runtime.base import RuntimeConversationIdentity, RuntimeHandle
from flowweave.runtime.child_governance import NativeGovernedChildPreparation
from flowweave.runtime.routing import runtime_for
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class RecoveryChild:
    binding_id: str
    conversation_id: str
    creation_id: UUID
    action_id: str
    tool_call_id: str
    request_digest: str
    persistence_dir: str
    provisioning: bool


@dataclass(frozen=True, slots=True)
class RecoveryFamily:
    capability: GovernedParentCapability
    children: tuple[RecoveryChild, ...]


def mismatch() -> DomainError:
    return DomainError(
        "AGENT_CHILD_RECOVERY_IDENTITY_MISMATCH", "父子会话原生身份恢复校验失败，数据已保留", 409
    )


def frozen_workspace_image(db: Session, runtime_session_id: str) -> str | None:
    """Infrastructure may inspect all users' frozen contracts, never their messages."""
    with tenant_bypass():
        bindings = list(
            db.scalars(
                select(AgentConversationBinding).where(
                    AgentConversationBinding.host_kind == "AGENT_WORKSPACE",
                    AgentConversationBinding.runtime_session_id == runtime_session_id,
                    AgentConversationBinding.child_parent_manifest_json["profile"].as_string()
                    == "OH_FW_V4",
                )
            )
        )
        images = {read_parent_capability(binding).runtime_image_digest for binding in bindings}
    if len(images) > 1:
        raise mismatch()
    return next(iter(images), None)


def collect(db: Session, runtime_session_id: str) -> tuple[RecoveryFamily, ...]:
    """Capture immutable governance metadata after routing has been fenced."""
    families: list[RecoveryFamily] = []
    with tenant_bypass():
        parents = list(
            db.scalars(
                select(AgentConversationBinding)
                .where(
                    AgentConversationBinding.runtime_session_id == runtime_session_id,
                    AgentConversationBinding.lifecycle.in_(("ACTIVE", "PROVISIONING")),
                    AgentConversationBinding.child_parent_manifest_json["profile"].as_string()
                    == "OH_FW_V4",
                )
                .order_by(AgentConversationBinding.id)
            )
        )
    for parent in parents:
        with tenant_user(parent.owner_user_id):
            frozen = read_parent_capability(parent)
            if not isinstance(frozen, GovernedParentCapability):
                raise mismatch()
            relations = list(
                db.scalars(
                    select(AgentChildConversation)
                    .where(AgentChildConversation.parent_binding_id == parent.id)
                    .order_by(AgentChildConversation.child_binding_id)
                )
            )
            children: list[RecoveryChild] = []
            for relation in relations:
                host = AgentSessionHostContext.create(
                    host_kind=parent.host_kind,
                    host_id=parent.host_id,
                    conversation_scope_id=parent.conversation_scope_id,
                    runtime_session_id=parent.runtime_session_id,
                    working_directory=frozen.working_directory,
                )
                _, _, child = require_direct_child(
                    db,
                    host,
                    parent_binding_id=parent.id,
                    child_binding_id=relation.child_binding_id,
                    allow_provisioning_parent=True,
                )
                if (
                    relation.creation_policy_version != POLICY_VERSION
                    or relation.accepted_generation != frozen.policy.generation
                    or relation.manifest_digest != frozen.policy.manifest_digest
                    or not relation.expected_persistence_dir
                    or not relation.request_digest
                ):
                    raise mismatch()
                children.append(
                    RecoveryChild(
                        child.id,
                        child.openhands_conversation_id,
                        UUID(relation.creation_id),
                        relation.action_id,
                        relation.tool_call_id,
                        relation.request_digest,
                        relation.expected_persistence_dir,
                        child.lifecycle == "PROVISIONING",
                    )
                )
            if children:
                families.append(RecoveryFamily(frozen, tuple(children)))
    return tuple(families)


def probe(
    families: tuple[RecoveryFamily, ...],
    resource_name: str,
    expected: dict[str, RuntimeConversationIdentity] | None = None,
) -> dict[str, RuntimeConversationIdentity]:
    """Reload existing IDs and the audited creation actions, without creating or running."""
    identities: dict[str, RuntimeConversationIdentity] = {}
    previous = expected or {}
    for family in families:
        frozen = family.capability
        parent_id = str(frozen.policy.parent_conversation_id)
        with tenant_user(str(frozen.owner_user_id)):
            workspace_root = (
                user_runtime_project_root()
                if frozen.host_kind == "AGENT_WORKSPACE"
                else frozen.working_directory
            )
        handle = RuntimeHandle(
            f"env-chat:{resource_name}",
            parent_id,
            runtime_resource_name=resource_name,
            workspace_root=workspace_root,
            protocol_profile="OH_FW_V4",
        )
        runtime = runtime_for("openhands", handle)
        parent_config = runtime.parent_conversation_configuration(handle)
        parent_identity = runtime.reload_conversation(handle, expected=previous.get(parent_id))
        if (
            str(parent_config.id) != parent_id
            or parent_identity.conversation_id != parent_id
            or parent_identity.persistence_dir != parent_config.persistence_dir
            or parent_config.workspace.working_dir != frozen.working_directory
            or parent_identity.workspace_working_dir != frozen.working_directory
            or parent_identity.event_id is None
        ):
            raise mismatch()
        identities[parent_id] = parent_identity
        for child in family.children:
            action = runtime.read_event(handle, child.action_id)
            if (
                action is None
                or action.cursor != child.action_id
                or action.event_type != "TOOL_CALL"
                or action.payload.get("action_id") != child.action_id
                or action.payload.get("tool_call_id") != child.tool_call_id
            ):
                raise mismatch()
            try:
                prepared = runtime.read_child_preparation(handle, child.creation_id)
            except DomainError as exc:
                if (
                    exc.code != "RUNTIME_CONVERSATION_MISSING"
                    or not child.provisioning
                    or child.conversation_id in previous
                ):
                    raise
                # A platform reservation can precede native materialization.
                # Preserve it without fabricating an empty Conversation.
                continue
            if (
                not isinstance(prepared, NativeGovernedChildPreparation)
                or str(prepared.receipt.identity.parent_conversation_id) != parent_id
                or str(prepared.receipt.identity.conversation_id) != child.conversation_id
                or prepared.receipt.identity.creation_id != child.creation_id
                or prepared.receipt.request_digest != child.request_digest
            ):
                raise mismatch()
            if prepared.phase == "reserved" and child.provisioning:
                if child.conversation_id in previous:
                    raise mismatch()
                continue
            if (
                str(prepared.governance_id) != child.binding_id
                or prepared.workspace != frozen.working_directory
            ):
                raise mismatch()
            child_handle = RuntimeHandle(
                handle.job_id,
                child.conversation_id,
                runtime_resource_name=resource_name,
                workspace_root=workspace_root,
                protocol_profile="OH_FW_V4",
            )
            config = runtime.child_conversation_configuration(child_handle)
            prior_child = previous.get(child.conversation_id)
            identity = runtime.reload_conversation(
                child_handle, expected=prior_child if prior_child and prior_child.event_id else None
            )
            if (
                str(config.id) != child.conversation_id
                or identity.conversation_id != child.conversation_id
                or str(config.parent_conversation_id) != parent_id
                or config.workspace.working_dir != frozen.working_directory
                or config.persistence_dir != child.persistence_dir
                or identity.workspace_working_dir != frozen.working_directory
                or identity.persistence_dir != child.persistence_dir
                or (prepared.phase == "started" and identity.event_id is None)
            ):
                raise mismatch()
            identities[child.conversation_id] = identity
    return identities
