"""Platform-managed capabilities visible to governance but unavailable to users."""

from typing import Any

from flowweave.shared.errors import DomainError

PLATFORM_MANAGED_CAPABILITY_KEYS = frozenset(
    {
        "flowweave-case-context",
        "flowweave-case-flows",
        "flowweave-case-materials",
        "flowweave-reference-history",
    }
)

# Exact Authorization references that platform MCP servers may carry. They are
# Secret Registry placeholders, never credentials.
PLATFORM_MCP_SECRET_REFERENCES = {
    "flowweave-reference-history": "Bearer ${FLOWWEAVE_HISTORY_ACCESS}",
    "flowweave-case-context": "Bearer ${FLOWWEAVE_CASE_ACCESS}",
    "flowweave-case-flows": "Bearer ${FLOWWEAVE_CASE_FLOW_ACCESS}",
    "flowweave-case-materials": "Bearer ${FLOWWEAVE_CASE_MATERIAL_ACCESS}",
}


def is_platform_managed_capability(capability_type: str, capability_key: str) -> bool:
    return capability_type == "MCP" and capability_key in PLATFORM_MANAGED_CAPABILITY_KEYS


def require_user_configurable_capability(published: Any) -> None:
    if is_platform_managed_capability(
        published.package.capability_type, published.package.capability_key
    ):
        raise DomainError(
            "CAPABILITY_PLATFORM_MANAGED",
            "该能力由平台按业务上下文自动装配，不能加入普通会话配置",
            422,
            {"capability_version_id": published.version.id},
        )
