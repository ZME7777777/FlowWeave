"""Apply FlowWeave's fail-closed ambient Plugin switch to OpenHands 1.40.0.

OpenHands 1.40.0 always discovers installed, user, and project Plugins when a
local conversation is initialized.  That behavior is appropriate for an
interactive developer installation, but it bypasses FlowWeave's immutable
Capability Version and Snapshot boundaries.

This build-time patch is intentionally strict: every upstream source fragment
must occur exactly once and every patched fragment must be present afterwards.
Any wheel drift therefore fails the Runtime image build instead of silently
re-enabling ambient Plugins.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
from pathlib import Path


def _replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text(encoding="utf-8")
    if after in source:
        if source.count(after) != 1:
            raise RuntimeError(f"{path}: patched fragment is not unique")
        return
    occurrences = source.count(before)
    if occurrences != 1:
        raise RuntimeError(
            f"{path}: expected one upstream fragment, found {occurrences}"
        )
    updated = source.replace(before, after, 1)
    ast.parse(updated, filename=str(path))
    path.write_text(updated, encoding="utf-8")


def _replace_text_once(path: Path, before: str, after: str) -> None:
    source = path.read_text(encoding="utf-8")
    if after in source:
        return
    if source.count(before) != 1:
        raise RuntimeError(f"{path}: governed text fragment is not unique")
    path.write_text(source.replace(before, after, 1), encoding="utf-8")


def _site_packages_root() -> Path:
    spec = importlib.util.find_spec("openhands")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("the pinned OpenHands packages are not installed")
    return Path(next(iter(spec.submodule_search_locations))).parent


def apply(root: Path) -> None:
    if (root / "openhands-sdk").is_dir():
        request = root / "openhands-sdk/openhands/sdk/conversation/request.py"
        event_service = root / "openhands-agent-server/openhands/agent_server/event_service.py"
        local_conversation = (
            root
            / "openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py"
        )
    else:
        request = root / "openhands/sdk/conversation/request.py"
        event_service = root / "openhands/agent_server/event_service.py"
        local_conversation = (
            root / "openhands/sdk/conversation/impl/local_conversation.py"
        )
    for path in (request, event_service, local_conversation):
        if not path.is_file():
            raise RuntimeError(f"missing pinned OpenHands source: {path}")

    if (root / "openhands-sdk").is_dir():
        _replace_text_once(
            root / "openhands-agent-server/pyproject.toml",
            '''  "docker/wallpaper.svg",
''',
            '''  "docker/wallpaper.svg",
  "openhands-source-provenance.json",
''',
        )

    _replace_once(
        request,
        '''    plugins: list[PluginSource] | None = Field(
        default=None,
        description=(
            "List of plugins to load for this conversation. Plugins are loaded "
            "and their skills/MCP config are merged into the agent. "
            "Hooks are extracted and stored for runtime execution."
        ),
    )
''',
        '''    plugins: list[PluginSource] | None = Field(
        default=None,
        description=(
            "List of plugins to load for this conversation. Plugins are loaded "
            "and their skills/MCP config are merged into the agent. "
            "Hooks are extracted and stored for runtime execution."
        ),
    )
    load_ambient_plugins: bool = Field(
        default=True,
        description=(
            "Whether installed, user, and project Plugins may be discovered "
            "in addition to explicitly attached Plugins."
        ),
    )
''',
    )
    _replace_once(
        event_service,
        '''            plugins=self.stored.plugins,
            persistence_dir=str(self.conversations_dir),
''',
        '''            plugins=self.stored.plugins,
            load_ambient_plugins=self.stored.load_ambient_plugins,
            persistence_dir=str(self.conversations_dir),
''',
    )
    _replace_once(
        local_conversation,
        '''    _plugin_specs: list[PluginSource] | None
    _resolved_plugins: list[ResolvedPluginSource] | None
''',
        '''    _plugin_specs: list[PluginSource] | None
    _load_ambient_plugins: bool
    _resolved_plugins: list[ResolvedPluginSource] | None
''',
    )
    _replace_once(
        local_conversation,
        '''        file_store: FileStore | None = None,
        mcp_tool_provider: MCPToolProvider | None = None,
        **_: object,
''',
        '''        file_store: FileStore | None = None,
        mcp_tool_provider: MCPToolProvider | None = None,
        load_ambient_plugins: bool = True,
        **_: object,
''',
    )
    _replace_once(
        local_conversation,
        '''        self._plugin_specs = plugins
        self._resolved_plugins = None
''',
        '''        self._plugin_specs = plugins
        self._load_ambient_plugins = load_ambient_plugins
        self._resolved_plugins = None
''',
    )
    _replace_once(
        local_conversation,
        '''                plugins=self._plugin_specs,
                persistence_dir=fork_persistence,
''',
        '''                plugins=self._plugin_specs,
                load_ambient_plugins=self._load_ambient_plugins,
                persistence_dir=fork_persistence,
''',
    )
    _replace_once(
        local_conversation,
        '''        ambient_plugins_loaded = False
        try:
            ambient_plugins = load_available_plugins(
                work_dir=self.workspace.working_dir,
                include_user=True,
                include_project=True,
            )
        except Exception:
            logger.warning(
                "Failed to load ambient (installed/local) plugins; "
                "continuing without them",
                exc_info=True,
            )
            ambient_plugins = {}
''',
        '''        ambient_plugins_loaded = False
        ambient_plugins = {}
        if self._load_ambient_plugins:
            try:
                ambient_plugins = load_available_plugins(
                    work_dir=self.workspace.working_dir,
                    include_user=True,
                    include_project=True,
                )
            except Exception:
                logger.warning(
                    "Failed to load ambient (installed/local) plugins; "
                    "continuing without them",
                    exc_info=True,
                )
''',
    )

    request_source = request.read_text(encoding="utf-8")
    event_source = event_service.read_text(encoding="utf-8")
    local_source = local_conversation.read_text(encoding="utf-8")
    required = {
        request: ("load_ambient_plugins: bool = Field(",),
        event_service: (
            "load_ambient_plugins=self.stored.load_ambient_plugins",
        ),
        local_conversation: (
            "self._load_ambient_plugins = load_ambient_plugins",
            "if self._load_ambient_plugins:",
            "load_ambient_plugins=self._load_ambient_plugins",
        ),
    }
    sources = {
        request: request_source,
        event_service: event_source,
        local_conversation: local_source,
    }
    for path, fragments in required.items():
        for fragment in fragments:
            if sources[path].count(fragment) != 1:
                raise RuntimeError(f"{path}: patched invariant is not unique: {fragment}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="Site-packages root; defaults to the active interpreter's install.",
    )
    args = parser.parse_args()
    apply((args.site_packages or _site_packages_root()).resolve())


if __name__ == "__main__":
    main()
