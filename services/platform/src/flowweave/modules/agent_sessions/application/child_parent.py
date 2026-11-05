"""Versioned creation-time parent authority, without a child execution lifecycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.application.child_relations import require_ordinary_binding
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentSidebarConversation,
)
from flowweave.modules.users.application.security import current_user_id
from flowweave.runtime.base import RuntimeAgentSpec, RuntimeTool
from flowweave.runtime.child_contract import (
    CHILD_TOOL_NAMES,
    V3_ARCHIVE_SHA256,
    V3_SOURCE_COMMIT,
    child_capability_manifest,
    native_child_tool_entries,
    require_child_capability_manifest,
)
from flowweave.runtime.child_creation import NativeChildPolicy
from flowweave.runtime.child_governance_contract import (
    GOVERNED_CHILD_SOURCES,
    V4_SOURCE_COMMIT,
    V5_SOURCE_COMMIT,
    require_governed_child_capability_manifest,
)
from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.runtime.protocol_profiles import runtime_contract_for_protocol_profile
from flowweave.shared.domain.openhands import OpenHandsServerIdentity
from flowweave.shared.errors import DomainError
from flowweave.shared.settings import get_settings

LAUNCH_PATH = "/api/v1/agent-child-launchers"


class ParentCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: Literal["OH_FW_V3"] = "OH_FW_V3"
    owner_user_id: UUID
    binding_id: UUID
    host_kind: Literal["AGENT_WORKSPACE", "FLOW_NODE"]
    host_id: UUID
    conversation_scope_id: UUID
    working_directory: str = Field(min_length=1)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_commit: Literal["3a4f5f1f61082430e8a2ccf8d59e52c9f4eab5dc"] = V3_SOURCE_COMMIT
    source_archive_sha256: str
    capability: dict[str, Any]
    runtime_manifest: dict[str, Any]
    policy: NativeChildPolicy
    launch_url: str
    query_url: str
    command_url: str


class GovernedParentCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    profile: Literal["OH_FW_V4"] = "OH_FW_V4"
    owner_user_id: UUID
    binding_id: UUID
    host_kind: Literal["AGENT_WORKSPACE", "FLOW_NODE"]
    host_id: UUID
    conversation_scope_id: UUID
    working_directory: str = Field(min_length=1)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_commit: Literal["b9f976b2ca05b947ee28eb34ebe590ff0409307f"] = V4_SOURCE_COMMIT
    source_archive_sha256: str
    capability: dict[str, Any]
    runtime_manifest: dict[str, Any]
    policy: NativeChildPolicy
    launch_url: str
    query_url: str
    command_url: str


class CurrentGovernedParentCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
    profile: Literal["OH_FW_V5"] = "OH_FW_V5"
    owner_user_id: UUID
    binding_id: UUID
    host_kind: Literal["AGENT_WORKSPACE", "FLOW_NODE"]
    host_id: UUID
    conversation_scope_id: UUID
    working_directory: str = Field(min_length=1)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_commit: Literal["805bb3fe0a57cdc3e383e26019fa216a6892e608"] = V5_SOURCE_COMMIT
    source_archive_sha256: str
    capability: dict[str, Any]
    runtime_manifest: dict[str, Any]
    policy: NativeChildPolicy
    launch_url: str
    query_url: str
    command_url: str


FrozenParentCapability = (
    ParentCapability | GovernedParentCapability | CurrentGovernedParentCapability
)


def denied() -> DomainError:
    return DomainError("AGENT_CHILD_LAUNCH_DENIED", "子会话调用未获授权", 403)


def gateway_urls(owner: str, binding_id: str) -> tuple[str, str, str]:
    base = get_settings().agent_child_gateway_base_url.rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DomainError("AGENT_CHILD_GATEWAY_UNCONFIGURED", "尚未配置可信子会话入口", 409)
    root = f"{base}{LAUNCH_PATH}/{UUID(owner)}/{UUID(binding_id)}"
    return root + "/start", root + "/queries", root + "/commands"


def read_parent_capability(binding: AgentConversationBinding) -> FrozenParentCapability:
    try:
        raw = binding.child_parent_manifest_json
        if isinstance(raw, dict) and raw.get("profile") in GOVERNED_CHILD_SOURCES:
            model = (
                CurrentGovernedParentCapability
                if raw["profile"] == "OH_FW_V5"
                else GovernedParentCapability
            )
            frozen = model.model_validate(raw)
            require_governed_child_capability_manifest(frozen.capability, frozen.profile)
            archive_digest = GOVERNED_CHILD_SOURCES[frozen.profile][1]
        else:
            frozen = ParentCapability.model_validate(raw)
            require_child_capability_manifest(frozen.capability)
            archive_digest = V3_ARCHIVE_SHA256
        if (
            frozen.source_archive_sha256 != archive_digest
            or frozen.runtime_manifest.get("child_conversations") != frozen.capability
            or frozen.policy.manifest_digest != runtime_manifest_hash(frozen.runtime_manifest)
            or str(frozen.owner_user_id) != binding.owner_user_id
            or str(frozen.binding_id) != binding.id
            or frozen.host_kind != binding.host_kind
            or str(frozen.host_id) != binding.host_id
            or str(frozen.conversation_scope_id) != binding.conversation_scope_id
            or str(frozen.policy.scope_id) != binding.id
            or str(frozen.policy.runtime_session_id) != binding.runtime_session_id
            or str(frozen.policy.parent_conversation_id) != binding.openhands_conversation_id
            or frozen.working_directory != binding.working_directory
            or (binding.host_kind == "AGENT_WORKSPACE" and binding.workspace_id != binding.host_id)
            or (binding.host_kind == "FLOW_NODE" and binding.flow_run_id != binding.host_id)
        ):
            raise denied()
        return frozen
    except (ValidationError, ValueError, TypeError, DomainError):
        raise denied() from None


def attach_parent_capability(
    db: Session,
    binding: AgentConversationBinding,
    spec: RuntimeAgentSpec,
    *,
    profile: str,
    identity: OpenHandsServerIdentity,
    generation: int,
    runtime_image_digest: str,
    runtime_manifest: dict[str, Any],
    expected_manifest_digest: str | None = None,
) -> RuntimeAgentSpec:
    if profile not in {"OH_FW_V3", "OH_FW_V4", "OH_FW_V5"}:
        if binding.child_parent_manifest_json is not None:
            raise denied()
        return spec
    expected_contract = runtime_contract_for_protocol_profile(
        profile, spec.runtime_contract.required_tools if spec.runtime_contract else (), identity
    )
    if spec.runtime_contract != expected_contract or get_settings().runtime_adapter != "openhands":
        raise denied()
    if (
        expected_manifest_digest is not None
        and runtime_manifest_hash(runtime_manifest) != expected_manifest_digest
    ):
        raise denied()
    capability = (
        require_governed_child_capability_manifest(
            runtime_manifest.get("child_conversations"), profile
        )
        if profile in {"OH_FW_V4", "OH_FW_V5"}
        else require_child_capability_manifest(runtime_manifest.get("child_conversations"))
    )
    require_ordinary_binding(db, binding)
    if binding.create_idempotency_key.startswith("gate-sidecar:") or db.scalar(
        select(AgentSidebarConversation.id).where(
            AgentSidebarConversation.sidebar_binding_id == binding.id
        )
    ):
        raise denied()
    if binding.child_parent_manifest_json is None:
        if binding.lifecycle != "PROVISIONING" or binding.owner_user_id != current_user_id():
            raise denied()
        launch_url, query_url, command_url = gateway_urls(binding.owner_user_id, binding.id)
        parent_model = {
            "OH_FW_V3": ParentCapability,
            "OH_FW_V4": GovernedParentCapability,
            "OH_FW_V5": CurrentGovernedParentCapability,
        }[profile]
        value = parent_model(
            owner_user_id=UUID(binding.owner_user_id),
            binding_id=UUID(binding.id),
            host_kind=cast(Literal["AGENT_WORKSPACE", "FLOW_NODE"], binding.host_kind),
            host_id=UUID(binding.host_id),
            conversation_scope_id=UUID(binding.conversation_scope_id),
            working_directory=binding.working_directory or "",
            runtime_image_digest=runtime_image_digest,
            source_archive_sha256=capability["source_archive_sha256"],
            capability=capability,
            runtime_manifest=deepcopy(runtime_manifest),
            policy=NativeChildPolicy(
                parent_conversation_id=UUID(binding.openhands_conversation_id),
                scope_id=UUID(binding.id),
                runtime_session_id=UUID(binding.runtime_session_id),
                generation=generation,
                manifest_digest=runtime_manifest_hash(runtime_manifest),
                max_active_children=3,
            ),
            launch_url=launch_url,
            query_url=query_url,
            command_url=command_url,
        )
        binding.child_parent_manifest_json = value.model_dump(mode="json")
        db.flush()
    frozen = read_parent_capability(binding)
    if (
        frozen.profile != profile
        or frozen.policy.generation != generation
        or frozen.runtime_image_digest != runtime_image_digest
        or frozen.runtime_manifest != runtime_manifest
    ):
        raise denied()
    entries = native_child_tool_entries(
        # V4 retains V3's exact six tool parameter schemas. Its own immutable
        # capability has already been checked above; reuse only the compiler.
        manifest=child_capability_manifest(),
        parent_conversation_id=frozen.policy.parent_conversation_id,
        scope_id=frozen.policy.scope_id,
        runtime_session_id=frozen.policy.runtime_session_id,
        generation=generation,
        manifest_digest=frozen.policy.manifest_digest,
        launch_url=frozen.launch_url,
        query_url=frozen.query_url,
        command_url=frozen.command_url,
    )
    if any(tool.name in {*CHILD_TOOL_NAMES, "start_child_conversation"} for tool in spec.tools):
        raise denied()
    return replace(
        spec,
        tools=spec.tools + tuple(RuntimeTool(entry["name"], entry["params"]) for entry in entries),
    )


def parent_protocol_profile(binding: AgentConversationBinding) -> str | None:
    return (
        read_parent_capability(binding).profile
        if binding.child_parent_manifest_json is not None
        else None
    )


def require_forkable_parent(binding: AgentConversationBinding) -> None:
    # Native fork copies resolved tool policy, which is bound to the source UUID.
    # Do not create an apparently new parent with the old parent's authority.
    if binding.child_parent_manifest_json is not None:
        raise DomainError(
            "AGENT_CHILD_PARENT_FORK_UNSUPPORTED", "当前子会话父能力不能复制到分叉会话", 409
        )
