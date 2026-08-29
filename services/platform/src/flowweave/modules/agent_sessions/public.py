"""Public entry point for the single Agent-session application implementation."""

from typing import Any

from flowweave.modules.agent_sessions.application.host import (
    ACCESS_FILES,
    ACCESS_TERMINAL,
    CONTROL_SESSIONS,
    CREATE_SESSIONS,
    LIST_SESSIONS,
    READ_SESSIONS,
    WRITE_SESSIONS,
    AgentSessionHostContext,
)
from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    AgentConversationCommand,
    AgentConversationMessageAttachment,
)


def process_agent_conversation_title(*args: Any, **kwargs: Any) -> None:
    """Run the shared title task without forcing the session core to import."""

    from flowweave.modules.agent_sessions.application.titles import (
        process_agent_conversation_title as process,
    )

    process(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """Load application modules only for callers that need their services.

    Host ORM models import this facade while the shared session service imports
    the host facade. Keeping service modules lazy prevents that legitimate
    dependency direction from becoming an import-time cycle.
    """

    if name in {"conversations", "titles"}:
        from flowweave.modules.agent_sessions.application import conversations, titles

        return {"conversations": conversations, "titles": titles}[name]
    if name in {"FlowNodeSessionHost", "resolve_flow_node_session_host"}:
        from flowweave.modules.agent_sessions.application.flow_node_host import (
            FlowNodeSessionHost,
            resolve_flow_node_session_host,
        )

        return {
            "FlowNodeSessionHost": FlowNodeSessionHost,
            "resolve_flow_node_session_host": resolve_flow_node_session_host,
        }[name]
    raise AttributeError(name)

__all__ = [
    "AgentConversationBinding",
    "AgentConversationCapability",
    "AgentConversationCommand",
    "AgentConversationMessageAttachment",
    "AgentSessionHostContext",
    "ACCESS_FILES",
    "ACCESS_TERMINAL",
    "CONTROL_SESSIONS",
    "CREATE_SESSIONS",
    "LIST_SESSIONS",
    "READ_SESSIONS",
    "WRITE_SESSIONS",
    "process_agent_conversation_title",
]
