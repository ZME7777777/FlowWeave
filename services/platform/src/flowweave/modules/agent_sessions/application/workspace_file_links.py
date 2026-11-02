from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import unquote

# A sandbox URL is a complete destination, not an ASCII filename prefix.
# Keep Markdown delimiters outside the match and allow balanced parentheses in
# filenames. File routes remain responsible for binding authorization and scope.
_SANDBOX_WORKSPACE_FILE = re.compile(
    r"""sandbox:(/runtime/workspace/(?:project|[0-9a-f-]{36})/"""
    r"""(?:[^\s<>"'`()\\]|\([^\s<>"'`()\\]*\))+)(?=$|[\s<>"'`)])"""
)


def is_runtime_workspace_directory(path: str) -> bool:
    """Accept a binding's canonical working directory, including nested projects."""
    parsed = PurePosixPath(path)
    return (
        parsed.as_posix() == path
        and ".." not in parsed.parts
        and re.fullmatch(r"/runtime/workspace/(?:project|[0-9a-f-]{36})(?:/[^/]+)*", path)
        is not None
    )


def project_sandbox_file_links(content: str, file_url: Callable[[str], str]) -> str:
    """Project complete Runtime-local file URLs without changing native events."""

    def replace(match: re.Match[str]) -> str:
        try:
            path = unquote(match.group(1), errors="strict")
        except UnicodeDecodeError:
            return match.group(0)
        return file_url(path)

    return _SANDBOX_WORKSPACE_FILE.sub(replace, content)
