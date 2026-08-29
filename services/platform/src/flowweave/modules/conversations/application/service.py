"""Compatibility import for the FlowRun host session service.

The active implementation belongs to :mod:`agent_sessions`; this path remains
only so existing integrations do not receive a second implementation.
"""

import sys

from flowweave.modules.agent_sessions import public as _agent_sessions

sys.modules[__name__] = _agent_sessions.flow_node_conversations
