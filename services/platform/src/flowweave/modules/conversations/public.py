"""Compatibility facade for FlowRun-hosted shared sessions."""

from typing import Any

from flowweave.modules.agent_sessions import public as _agent_sessions

_LOCATOR = _agent_sessions.flow_node_locator
_CONVERSATIONS = _agent_sessions.flow_node_conversations


def __getattr__(name: str) -> Any:
    """Forward legacy FlowRun session imports to the shared implementation."""

    for module in (_LOCATOR, _CONVERSATIONS):
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(name)
