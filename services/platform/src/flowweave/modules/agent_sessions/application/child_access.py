"""Native Tool callback authorization. Runtime proof never replaces user scope."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application import child_launcher, child_sessions
from flowweave.modules.agent_sessions.application.child_parent import denied
from flowweave.modules.agent_sessions.infrastructure.models import AgentChildConversation
from flowweave.modules.users.application.security import tenant_user
from flowweave.runtime.child_interaction import (
    NativeChildSnapshot,
    NativeCommandInvocation,
    NativeCommandRequest,
    NativeCommandResult,
    NativeListPage,
    NativeQueryInvocation,
    NativeQueryRequest,
    NativeQueryResult,
)
from flowweave.runtime.dependencies import get_runtime
from flowweave.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class AuthorizedAccess:
    parent: child_launcher.AuthorizedParent
    claim: NativeQueryInvocation | NativeCommandInvocation
    child: child_sessions.ChildLocator | None


def verify(
    parent: child_launcher.AuthorizedParent, request: NativeQueryRequest | NativeCommandRequest
) -> NativeQueryInvocation | NativeCommandInvocation:
    if parent.capability.profile != "OH_FW_V4":
        raise denied()
    runtime = get_runtime()
    return (
        runtime.verify_child_query(parent.handle, request)
        if isinstance(request, NativeQueryRequest)
        else runtime.verify_child_command(parent.handle, request)
    )


def authorize(
    db: Session,
    parent: child_launcher.AuthorizedParent,
    claim: NativeQueryInvocation | NativeCommandInvocation,
    key: SecretStr,
) -> AuthorizedAccess:
    with tenant_user(str(parent.capability.owner_user_id)):
        fresh = child_launcher.prepare_launcher(
            db,
            owner_id=parent.capability.owner_user_id,
            binding_id=parent.capability.binding_id,
            runtime_key=key,
        )
        audience = (
            fresh.capability.query_url
            if isinstance(claim, NativeQueryInvocation)
            else fresh.capability.command_url
        )
        if (
            fresh != parent
            or fresh.capability.profile != "OH_FW_V4"
            or claim.policy != fresh.capability.policy
            or claim.audience != audience
        ):
            raise denied()
        target = (
            claim.query.conversation_id
            if isinstance(claim, NativeQueryInvocation)
            else claim.command.conversation_id
        )
        locator = None
        if target is not None:
            relation = db.scalar(
                select(AgentChildConversation).where(
                    AgentChildConversation.parent_binding_id == str(parent.capability.binding_id),
                    AgentChildConversation.child_conversation_id == str(target),
                )
            )
            if relation is None:
                raise denied()
            locator = child_sessions.resolve(
                db,
                str(parent.capability.binding_id),
                relation.child_binding_id,
                write=isinstance(claim, NativeCommandInvocation),
                native_parent=True,
            )
        return AuthorizedAccess(fresh, claim, locator)


def execute(
    access: AuthorizedAccess, request: NativeQueryRequest | NativeCommandRequest
) -> NativeQueryResult | NativeCommandResult:
    if access.child is not None:
        child_sessions.verify_native(access.child)
    runtime = get_runtime()
    return (
        runtime.query_children(access.parent.handle, request)
        if isinstance(request, NativeQueryRequest)
        else runtime.command_child(access.parent.handle, request)
    )


def project(
    db: Session,
    access: AuthorizedAccess,
    result: NativeQueryResult | NativeCommandResult,
    key: SecretStr,
) -> dict[str, object]:
    fresh = authorize(db, access.parent, access.claim, key)
    if fresh != access:
        raise denied()
    with tenant_user(str(access.parent.capability.owner_user_id)):
        if isinstance(result, NativeListPage):
            # Native pagination remains authoritative; filter runtime-only
            # children which have no governed platform locator for this parent.
            visible: list[NativeChildSnapshot] = []
            for item in result.items:
                relation = db.scalar(
                    select(AgentChildConversation).where(
                        AgentChildConversation.parent_binding_id
                        == str(access.parent.capability.binding_id),
                        AgentChildConversation.child_conversation_id == str(item.conversation_id),
                    )
                )
                if relation is not None and item.preparation_phase == "started":
                    try:
                        child_sessions.resolve(
                            db,
                            relation.parent_binding_id,
                            relation.child_binding_id,
                            native_parent=True,
                        )
                    except DomainError as error:
                        if error.status == 404:
                            continue
                        raise
                    visible.append(item)
            return result.model_copy(update={"items": visible}).model_dump(mode="json")
        if access.child is None:
            raise denied()
        identity = (
            result.conversation_id
            if isinstance(result, NativeCommandResult)
            else result.snapshot.conversation_id
        )
        if str(identity) != access.child.handle.conversation_id:
            raise denied()
        if (
            isinstance(result, NativeCommandResult)
            and isinstance(access.claim, NativeCommandInvocation)
            and access.claim.command.operation == result.operation == "send"
            and result.event_id
        ):
            binding = child_sessions.mutation_binding(db, access.child)
            binding.terminal_reconciliation_pending = True
            binding.final_reply_preview_event_id = None
            binding.final_reply_preview_content = None
            db.flush()
        return result.model_dump(mode="json")
