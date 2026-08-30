"""Compatibility data for historical Alembic revisions only.

It is not loaded by the capability repository or Runtime.
"""

from flowweave.shared.domain.openhands import OPENHANDS_SOURCE_COMMIT, OPENHANDS_VERSION

RETIRED_AGENT_CONFIG_KEY = "flowweave-default-tools"
RETIRED_AGENT_CONFIG: dict[str, object] = {
    "name": RETIRED_AGENT_CONFIG_KEY,
    "description": "Retired historical agent configuration",
    "schema_version": 2,
    "openhands_version": OPENHANDS_VERSION,
    "source_commit": OPENHANDS_SOURCE_COMMIT,
    "catalog_digest": "retired",
    "tools": [],
    "confirmation_required_tools": [],
    "tool_concurrency_limit": 1,
}
