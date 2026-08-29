"""Host-neutral authorization context for the shared Agent-session core.

Session behavior must never infer its Runtime, working directory or write
permission from a route parameter.  Each product host resolves those facts at
its boundary and passes this immutable context into the shared core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

AgentSessionPermission = str

LIST_SESSIONS: AgentSessionPermission = "LIST_SESSIONS"
READ_SESSIONS: AgentSessionPermission = "READ_SESSIONS"
CREATE_SESSIONS: AgentSessionPermission = "CREATE_SESSIONS"
WRITE_SESSIONS: AgentSessionPermission = "WRITE_SESSIONS"
ACCESS_FILES: AgentSessionPermission = "ACCESS_FILES"
ACCESS_TERMINAL: AgentSessionPermission = "ACCESS_TERMINAL"
CONTROL_SESSIONS: AgentSessionPermission = "CONTROL_SESSIONS"


@dataclass(frozen=True, slots=True)
class AgentSessionHostContext:
    """Verified, host-owned facts consumed by one shared session core.

    ``host_id`` and ``conversation_scope_id`` are authorization identities,
    not browser-selected values. ``runtime_session_id`` is the stable logical
    Runtime identity; a physical endpoint, generation or Secret must never be
    added here.
    """

    host_kind: str
    host_id: str
    conversation_scope_id: str
    runtime_session_id: str
    working_directory: str
    runtime_manifest: Mapping[str, Any]
    model_policy: Mapping[str, Any]
    permissions: frozenset[AgentSessionPermission]

    @classmethod
    def create(
        cls,
        *,
        host_kind: str,
        host_id: str,
        conversation_scope_id: str,
        runtime_session_id: str,
        working_directory: str,
        runtime_manifest: Mapping[str, Any] | None = None,
        model_policy: Mapping[str, Any] | None = None,
        permissions: frozenset[AgentSessionPermission] = frozenset(),
    ) -> AgentSessionHostContext:
        """Copy mappings so callers cannot mutate verified host facts later."""

        return cls(
            host_kind=host_kind,
            host_id=host_id,
            conversation_scope_id=conversation_scope_id,
            runtime_session_id=runtime_session_id,
            working_directory=working_directory,
            runtime_manifest=MappingProxyType(dict(runtime_manifest or {})),
            model_policy=MappingProxyType(dict(model_policy or {})),
            permissions=frozenset(permissions),
        )

    def permits(self, permission: AgentSessionPermission) -> bool:
        return permission in self.permissions


__all__ = (
    "ACCESS_FILES",
    "ACCESS_TERMINAL",
    "CONTROL_SESSIONS",
    "CREATE_SESSIONS",
    "LIST_SESSIONS",
    "READ_SESSIONS",
    "WRITE_SESSIONS",
    "AgentSessionHostContext",
    "AgentSessionPermission",
)
