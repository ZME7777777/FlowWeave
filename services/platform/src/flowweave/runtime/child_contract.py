"""Immutable V3 child capability, separate from per-binding native authority."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flowweave.runtime.manifest import runtime_manifest_hash
from flowweave.shared.errors import DomainError

# V3 remains tied to this audited extension even if the build baseline advances.
V3_SOURCE_COMMIT = "3a4f5f1f61082430e8a2ccf8d59e52c9f4eab5dc"
V3_ARCHIVE_SHA256 = "f63ecb0d046c1a6a20e39540c1cb6b37570c105c788916b782bc76c5881b3846"
V3_PACKAGE_VERSION = "1.58.2"
CHILD_TOOL_NAMES = (
    "start_governed_child_conversation",
    "list_child_conversations",
    "read_child_conversation",
    "wait_for_child_conversation",
    "send_child_conversation_message",
    "interrupt_child_conversation",
)
CHILD_HTTP_OPERATIONS = (
    ("POST", "/api/conversations/{conversation_id}/children/launcher/verify"),
    ("POST", "/api/conversations/{conversation_id}/children/preparations/{creation_id}"),
    ("GET", "/api/conversations/{conversation_id}/children/preparations/{creation_id}"),
    ("POST", "/api/conversations/{conversation_id}/children/preparations/{creation_id}/release"),
    ("POST", "/api/conversations/{conversation_id}/children/queries/verify"),
    ("POST", "/api/conversations/{conversation_id}/children/queries"),
    ("POST", "/api/conversations/{conversation_id}/children/commands/verify"),
    ("POST", "/api/conversations/{conversation_id}/children/commands"),
)
# Historical manifest metadata preserves V3/V4/V5 digests. Parent assembly does
# not append this guidance to the model context.
CHILD_USAGE_GUIDANCE = (
    "简单查找自行完成。需要子任务结果后继续推理时优先使用 Task；需要独立历史、并行工作或"
    "用户后续介入时使用子会话。提供完整任务、路径、约束和交付物，不重复委派同一任务。"
    "启动只返回身份，不代表完成；未知结果按原创建身份对账。主动 list/read/wait 跟踪直属子会话，"
    "尊重分页和完整性标记。send 不自动打断，busy/429 不代表已排队。子完成不自动唤醒父会话。"
)


def child_capability_manifest() -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "version": "oh-fw-child-v1",
        "source_commit": V3_SOURCE_COMMIT,
        "source_archive_sha256": V3_ARCHIVE_SHA256,
        "package_version": V3_PACKAGE_VERSION,
        "tools": list(CHILD_TOOL_NAMES),
        "isolation": "shared",
        "max_depth": 1,
        "max_active_children": 3,
        "wait_timeout_seconds": 30,
        "list_page_size": 50,
        "read_scan_events": 100,
        "read_max_chars": 12000,
        "user_submission_id_required": True,
        "prepare_before_release": True,
        "usage_guidance": CHILD_USAGE_GUIDANCE,
    }
    return {**document, "digest": runtime_manifest_hash(document)}


def require_child_capability_manifest(value: object) -> dict[str, Any]:
    expected = child_capability_manifest()
    try:
        matches = runtime_manifest_hash(value) == runtime_manifest_hash(expected)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise DomainError(
            "RUNTIME_CHILD_CAPABILITY_INCOMPATIBLE",
            "The V3 child capability manifest is absent or differs from its frozen contract",
            409,
        )
    return expected


def native_child_tool_entries(
    *,
    manifest: object,
    parent_conversation_id: UUID,
    scope_id: UUID,
    runtime_session_id: UUID,
    generation: int,
    manifest_digest: str,
    launch_url: str,
    query_url: str,
    command_url: str,
) -> list[dict[str, Any]]:
    """Compile formal native tool parameters after the host authorizes a binding.

    The host owns endpoint authorization and active-generation fencing. No
    identity or URL is supplied by the model or stored in the shared capability.
    """

    capability = require_child_capability_manifest(manifest)
    if (
        isinstance(generation, bool)
        or generation < 1
        or len(manifest_digest) != 64
        or any(char not in "0123456789abcdef" for char in manifest_digest)
    ):
        raise ValueError("Invalid native child policy identity")
    policy = {
        "parent_conversation_id": str(parent_conversation_id),
        "scope_id": str(scope_id),
        "runtime_session_id": str(runtime_session_id),
        "generation": generation,
        "manifest_digest": manifest_digest,
        "max_active_children": capability["max_active_children"],
    }
    return [
        {
            "name": name,
            "params": {
                "policy": dict(policy),
                **(
                    {"launch_url": launch_url}
                    if index == 0
                    else {"access_url": query_url if index < 4 else command_url}
                ),
            },
        }
        for index, name in enumerate(CHILD_TOOL_NAMES)
    ]
