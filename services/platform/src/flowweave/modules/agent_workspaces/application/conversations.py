"""Agent Workspace host compatibility facade for the shared session service.

The Agent Workspace is currently the only session host.  Keep its import path
stable while the complete conversation behavior lives in ``agent_sessions``;
future hosts must use that same implementation rather than copy it.
"""

import sys

from flowweave.modules.agent_sessions import public as _agent_sessions

# Preserve the historical module identity for current Agent Workspace callers.
# This is an alias, not a copied facade: monkeypatches and every function
# global resolve against the one shared implementation.
sys.modules[__name__] = _agent_sessions.conversations
