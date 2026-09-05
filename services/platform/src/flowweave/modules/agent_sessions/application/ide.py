"""SSH Remote descriptors for persistent Agent-session workspaces."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from flowweave.shared.settings import get_settings

_RUNTIME_PROJECT = PurePosixPath("/runtime/workspace/project")


def ssh_remote_descriptor(
    project_root: Path,
    working_directory: str,
    *,
    runtime_mount_root: PurePosixPath = _RUNTIME_PROJECT,
) -> dict[str, Any]:
    """Return a JetBrains Gateway-ready SSH descriptor when deployment enables it.

    Runtime workspace paths are container-internal. The client must instead
    open the matching persistent Docker-host path, derived from the configured
    host workspace root and the server-owned allocation-relative path.
    """

    settings = get_settings()
    host = settings.ide_ssh_host.strip()
    user = settings.ide_ssh_user.strip()
    remote_root = Path(settings.runtime_host_workspace_root)
    if not host or not user or not remote_root.is_absolute():
        return {
            "supported": False,
            "status": "未配置 SSH Remote",
            "note": "部署方配置 IDEA SSH 主机、用户和宿主机工作区根目录后可连接。",
        }

    local_root = Path(settings.workspace_root).absolute()
    try:
        local_root = local_root.resolve(strict=True)
        relative_project = project_root.resolve(strict=True).relative_to(local_root)
    except (OSError, ValueError):
        return {
            "supported": False,
            "status": "SSH 工作区不可用",
            "note": "当前工作区无法映射到宿主机持久目录。",
        }

    runtime_directory = PurePosixPath(working_directory)
    if (
        not runtime_directory.is_absolute()
        or not runtime_mount_root.is_absolute()
        or not runtime_directory.is_relative_to(runtime_mount_root)
        or runtime_directory.as_posix() != working_directory
    ):
        return {
            "supported": False,
            "status": "SSH 工作区不可用",
            "note": "当前工作目录不是可连接的项目工作区。",
        }

    relative_directory = runtime_directory.relative_to(runtime_mount_root)
    remote_path = remote_root.joinpath(relative_project, *relative_directory.parts)
    command = f"ssh -p {settings.ide_ssh_port} {user}@{host}"
    return {
        "supported": True,
        "status": "可通过 SSH 连接",
        "note": "在 JetBrains Gateway 中选择 SSH，并打开以下宿主机目录。",
        "transport": "SSH_REMOTE",
        "host": host,
        "port": settings.ide_ssh_port,
        "user": user,
        "path": str(remote_path),
        "ssh_command": command,
    }
