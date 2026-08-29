"""Compatibility alias for the shared Agent-session title service."""

import sys

from flowweave.modules.agent_sessions import public as _agent_sessions

sys.modules[__name__] = _agent_sessions.titles
