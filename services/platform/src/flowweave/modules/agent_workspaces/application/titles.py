"""Compatibility alias for the shared Agent-session title service."""

import sys

from flowweave.modules.agent_sessions.application import titles as _shared_titles

sys.modules[__name__] = _shared_titles
